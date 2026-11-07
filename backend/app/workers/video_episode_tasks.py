"""Serial ComfyUI generation for episodes and complete video series."""

import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import (
    RunningHubConfig,
    VideoEpisode,
    VideoEpisodeShot,
    VideoSeries,
    VideoSeriesEpisode,
)
from ..security import decrypt_secret
from ..services.comfy_client import ComfyClient, ComfyError
from ..services.runninghub import (
    RunningHubClient,
    RunningHubError,
    workflow_media_fields,
    workflow_parameter_node_info,
)
from ..services.task_reliability import classify_task_error
from ..services.video_episode import duration_segments
from ..services.video_workflow import build_video_workflow
from .celery_app import celery_app

logger = logging.getLogger(__name__)


class EpisodeCancelled(Exception):
    """Raised when a user cancels the episode while a segment is running."""


class EpisodeVideoError(Exception):
    """A user-actionable episode video generation or assembly error."""


def _relative_path(path: Path) -> str:
    return path.relative_to(settings.image_dir).as_posix()


def _episode_path(episode_id: int) -> Path:
    path = settings.image_dir / "episodes" / str(episode_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _asset_references(episode: VideoEpisode, data: dict) -> tuple[list[str], list[str], list[str]]:
    assets = {asset["id"]: asset for asset in json.loads(episode.assets_json)}
    images: list[str] = []
    videos: list[str] = []
    audios: list[str] = []
    for asset_id in data.get("asset_ids", []):
        asset = assets.get(asset_id)
        if asset is None:
            raise EpisodeVideoError("分镜引用的素材不存在，请重新导入素材包")
        kind = asset["media_type"]
        if kind == "image":
            images.append(asset["stored_filename"])
        elif kind == "video":
            videos.append(asset["stored_filename"])
        elif kind == "audio":
            audios.append(asset["stored_filename"])
    return images, videos, audios


def _runninghub_node_info(
    client: RunningHubClient,
    workflow_id: str,
    selection: dict,
    prompt: str,
    ref_images: list[Path],
    ref_videos: list[Path],
    ref_audios: list[Path],
    aspect_ratio: str,
    megapixels: float,
    duration_seconds: float,
) -> list[dict[str, str]]:
    workflow = client.get_workflow(workflow_id)
    node_info_list: list[dict[str, str]] = []

    def add_text_mapping(node_id: str, field_name: str, value: str, label: str) -> None:
        node = workflow.get(node_id)
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or not isinstance(inputs.get(field_name), str):
            raise EpisodeVideoError(f"RunningHub 工作流中的{label}输入已失效")
        node_info_list.append(
            {"nodeId": node_id, "fieldName": field_name, "fieldValue": value}
        )

    prompt_node_id = str(selection.get("prompt_node_id") or "")
    prompt_field = str(selection.get("prompt_field") or "")
    if not prompt_node_id or not prompt_field:
        raise EpisodeVideoError("RunningHub 视频工作流缺少正向提示词映射")
    add_text_mapping(prompt_node_id, prompt_field, prompt, "正向提示词")
    negative_node_id = selection.get("negative_prompt_node_id")
    negative_field = selection.get("negative_prompt_field")
    if negative_node_id and negative_field:
        add_text_mapping(
            str(negative_node_id),
            str(negative_field),
            "",
            "负向提示词",
        )
    node_info_list.extend(
        workflow_parameter_node_info(
            workflow,
            aspect_ratio,
            megapixels,
            seed=-1,
            duration_seconds=duration_seconds,
        )
    )

    media_nodes = workflow_media_fields(workflow)

    for kind, references in (
        ("image", ref_images),
        ("video", ref_videos),
        ("audio", ref_audios),
    ):
        if len(references) > len(media_nodes[kind]):
            label = {"image": "图片", "video": "视频", "audio": "音频"}[kind]
            raise EpisodeVideoError(
                f"所选 RunningHub 视频工作流的{label}"
                f"输入节点不足：需要 {len(references)} 个，可用 {len(media_nodes[kind])} 个"
            )
        for path, mapping in zip(references, media_nodes[kind], strict=False):
            if not path.is_file():
                raise EpisodeVideoError(f"参考素材文件已丢失：{path.name}")
            uploaded_name = client.upload_media(path)
            node_info_list.append(
                {
                    "nodeId": mapping["node_id"],
                    "fieldName": mapping["field_name"],
                    "fieldValue": uploaded_name,
                }
            )
    return node_info_list


def _generate_runninghub_segment(
    db,
    client: RunningHubClient,
    episode: VideoEpisode,
    workflow_id: str,
    selection: dict,
    prompt: str,
    ref_images: list[Path],
    ref_videos: list[Path],
    ref_audios: list[Path],
    shot_index: int,
    segment_index: int,
    aspect_ratio: str,
    megapixels: float,
    duration_seconds: float,
) -> str:
    settings.image_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f"runninghub-video-{episode.id}-{shot_index}-{segment_index}-",
            dir=settings.image_dir,
        )
    )
    try:
        db.commit()
        node_info_list = _runninghub_node_info(
            client,
            workflow_id,
            selection,
            prompt,
            ref_images,
            ref_videos,
            ref_audios,
            aspect_ratio,
            megapixels,
            duration_seconds,
        )
        db.commit()
        shot = (
            db.query(VideoEpisodeShot)
            .filter(
                VideoEpisodeShot.episode_id == episode.id,
                VideoEpisodeShot.shot_index == shot_index,
            )
            .first()
        )
        if shot is None:
            raise EpisodeVideoError("分镜任务记录已不存在")
        task_ids = json.loads(shot.runninghub_tasks_json or "[]")
        existing_task_id = (
            task_ids[segment_index]
            if segment_index < len(task_ids)
            else None
        )
        db.commit()

        def persist_task_id(remote_task_id: str | None) -> None:
            current_shot = (
                db.query(VideoEpisodeShot)
                .filter(
                    VideoEpisodeShot.episode_id == episode.id,
                    VideoEpisodeShot.shot_index == shot_index,
                )
                .first()
            )
            if current_shot is None:
                raise EpisodeVideoError("分镜任务记录已不存在")
            current_task_ids = json.loads(current_shot.runninghub_tasks_json or "[]")
            while len(current_task_ids) <= segment_index:
                current_task_ids.append(None)
            current_task_ids[segment_index] = remote_task_id
            current_shot.runninghub_tasks_json = json.dumps(current_task_ids)
            db.commit()

        result = client.generate(
            workflow_id,
            node_info_list,
            temp_dir,
            cancel_check=lambda: _check_episode_cancelled(episode.id),
            existing_task_id=existing_task_id,
            task_id_callback=persist_task_id,
        )
        videos = [item for item in result["files"] if item.get("kind") == "video"]
        if not videos:
            raise EpisodeVideoError("RunningHub 视频工作流未返回视频")
        meta = videos[0]
        content = Path(meta["path"]).read_bytes()
        return _save_clip(
            episode.id,
            shot_index,
            segment_index,
            meta["filename"],
            content,
        )
    finally:
        try:
            shutil.rmtree(temp_dir)
        except OSError:
            logger.exception(
                "清理 RunningHub 视频临时目录失败 episode_id=%s shot_index=%s segment_index=%s",
                episode.id,
                shot_index,
                segment_index,
            )


def _run_runninghub_episode_shots(
    episode_id: int,
    client: RunningHubClient,
    workflow_id: str,
    selection: dict,
) -> None:
    """Generate independent RunningHub shots concurrently, keeping each shot's segments ordered."""
    with SessionLocal() as db:
        episode = db.get(VideoEpisode, episode_id)
        if episode is None:
            raise EpisodeVideoError("单集记录已不存在")
        jobs = [
            {"shot_index": shot.shot_index, "shot_no": shot.shot_no}
            for shot in sorted(episode.shots, key=lambda item: item.shot_index)
            if shot.status != "completed"
        ]
        completed_count = sum(shot.status == "completed" for shot in episode.shots)
        total_shots = episode.total_shots

    def generate_shot(job: dict[str, int | str]) -> None:
        shot_index = int(job["shot_index"])
        with SessionLocal() as db:
            try:
                episode = db.get(VideoEpisode, episode_id)
                shot = (
                    db.query(VideoEpisodeShot)
                    .filter(
                        VideoEpisodeShot.episode_id == episode_id,
                        VideoEpisodeShot.shot_index == shot_index,
                    )
                    .first()
                )
                if episode is None or shot is None:
                    raise EpisodeVideoError("分镜任务记录已不存在")
                if episode.status == "cancelled":
                    raise EpisodeCancelled()

                data = json.loads(shot.data_json)
                planned_segments = duration_segments(
                    float(data["duration_seconds"])
                )
                generated_segments = json.loads(shot.segments_json or "[]")
                ref_images, ref_videos, ref_audios = _asset_references(episode, data)
                input_dir = settings.comfyui_input_path
                if (ref_images or ref_videos or ref_audios) and input_dir is None:
                    raise EpisodeVideoError("未配置 RunningHub 参考素材目录")
                ref_image_paths = [
                    input_dir / filename for filename in ref_images
                ] if input_dir is not None else []
                ref_video_paths = [
                    input_dir / filename for filename in ref_videos
                ] if input_dir is not None else []
                ref_audio_paths = [
                    input_dir / filename for filename in ref_audios
                ] if input_dir is not None else []

                shot.status = "processing"
                shot.error = None
                episode.current_shot_index = shot_index
                episode.progress_message = (
                    f"RunningHub 并行生成中：第 {shot_index + 1}/{total_shots} 镜"
                )
                db.commit()

                for segment_index, (generation_duration, target_duration) in enumerate(
                    planned_segments
                ):
                    if segment_index < len(generated_segments):
                        previous_file = (
                            settings.image_dir
                            / generated_segments[segment_index]["filepath"]
                        )
                        if not previous_file.is_file():
                            raise EpisodeVideoError(
                                f"镜头 {shot.shot_no} 的生成片段已丢失"
                            )
                        continue

                    _check_episode_cancelled(episode_id)
                    prompt = data.get("prompt") or data["visual_description"]
                    segment_video_paths = list(ref_video_paths)
                    if segment_index > 0:
                        previous = (
                            settings.image_dir
                            / generated_segments[segment_index - 1]["filepath"]
                        )
                        segment_video_paths = [previous, *ref_video_paths[:2]]
                        prompt += (
                            "\nContinue the same shot from the supplied reference video's "
                            "final moment. Preserve the same characters, scene, lighting, "
                            "and camera motion."
                        )

                    relative_path = _generate_runninghub_segment(
                        db,
                        client,
                        episode,
                        workflow_id,
                        selection,
                        prompt,
                        ref_image_paths,
                        segment_video_paths,
                        ref_audio_paths,
                        shot_index,
                        segment_index,
                        data["aspect_ratio"],
                        float(data["megapixels"]),
                        generation_duration,
                    )
                    generated_segments.append(
                        {
                            "filepath": relative_path,
                            "target_duration": target_duration,
                        }
                    )
                    current_episode = db.get(VideoEpisode, episode_id)
                    current_shot = (
                        db.query(VideoEpisodeShot)
                        .filter(
                            VideoEpisodeShot.episode_id == episode_id,
                            VideoEpisodeShot.shot_index == shot_index,
                        )
                        .first()
                    )
                    if current_episode is None or current_shot is None:
                        raise EpisodeVideoError("分镜任务记录已不存在")
                    current_shot.segments_json = json.dumps(
                        generated_segments,
                        ensure_ascii=False,
                    )
                    task_ids = json.loads(current_shot.runninghub_tasks_json or "[]")
                    if segment_index < len(task_ids):
                        task_ids[segment_index] = None
                        current_shot.runninghub_tasks_json = json.dumps(task_ids)
                    current_episode.current_segment_index = segment_index + 1
                    db.commit()

                shot = (
                    db.query(VideoEpisodeShot)
                    .filter(
                        VideoEpisodeShot.episode_id == episode_id,
                        VideoEpisodeShot.shot_index == shot_index,
                    )
                    .first()
                )
                episode = db.get(VideoEpisode, episode_id)
                if shot is None or episode is None:
                    raise EpisodeVideoError("分镜任务记录已不存在")
                shot.status = "completed"
                shot.error = None
                episode.progress_message = (
                    f"RunningHub 已完成第 {shot_index + 1}/{total_shots} 镜"
                )
                db.commit()
            except EpisodeCancelled:
                db.rollback()
                shot = (
                    db.query(VideoEpisodeShot)
                    .filter(
                        VideoEpisodeShot.episode_id == episode_id,
                        VideoEpisodeShot.shot_index == shot_index,
                    )
                    .first()
                )
                if shot is not None and shot.status == "processing":
                    shot.status = "cancelled"
                    db.commit()
                raise
            except Exception as exc:
                db.rollback()
                shot = (
                    db.query(VideoEpisodeShot)
                    .filter(
                        VideoEpisodeShot.episode_id == episode_id,
                        VideoEpisodeShot.shot_index == shot_index,
                    )
                    .first()
                )
                if shot is not None:
                    shot.status = "failed"
                    shot.error = str(exc)[:1000]
                    db.commit()
                raise

    max_workers = settings.runninghub_max_parallel_shots
    job_iter = iter(jobs)
    active = {}
    first_failure: tuple[dict[str, int | str], Exception] | None = None
    cancelled = False

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"runninghub-episode-{episode_id}",
    ) as executor:
        def submit_next() -> bool:
            try:
                job = next(job_iter)
            except StopIteration:
                return False
            active[executor.submit(generate_shot, job)] = job
            return True

        while len(active) < max_workers and submit_next():
            pass

        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                try:
                    future.result()
                except EpisodeCancelled:
                    cancelled = True
                except Exception as exc:
                    if first_failure is None:
                        first_failure = (job, exc)
                else:
                    completed_count += 1
                    with SessionLocal() as db:
                        episode = db.get(VideoEpisode, episode_id)
                        if episode is not None and episode.status != "cancelled":
                            episode.progress_message = (
                                f"RunningHub 镜头进度：{completed_count}/{total_shots} 已完成"
                            )
                            db.commit()
            if first_failure is None and not cancelled:
                while len(active) < max_workers and submit_next():
                    pass

    if cancelled:
        raise EpisodeCancelled()
    if first_failure is not None:
        job, exc = first_failure
        raise EpisodeVideoError(
            f"RunningHub 第 {job['shot_no']} 镜制作失败：{str(exc)[:500]}"
        ) from exc


def _save_clip(
    episode_id: int,
    shot_index: int,
    segment_index: int,
    filename: str,
    content: bytes,
) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".gif"}:
        suffix = ".mp4"
    target = (
        _episode_path(episode_id)
        / "clips"
        / f"shot-{shot_index + 1:03d}-segment-{segment_index + 1:03d}{suffix}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return _relative_path(target)


def _check_episode_cancelled(episode_id: int) -> None:
    with SessionLocal() as db:
        episode = db.get(VideoEpisode, episode_id)
        if episode is None or episode.status == "cancelled":
            raise EpisodeCancelled()


def _has_audio_stream(ffmpeg: str, clip_path: Path) -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(clip_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stderr or result.stdout
    if "Video:" not in output:
        raise EpisodeVideoError(f"生成片段无法读取：{clip_path.name}")
    return "Audio:" in output


def _output_size(aspect_ratio: str, megapixels: float) -> tuple[int, int]:
    try:
        width_ratio, height_ratio = aspect_ratio.split()[0].split(":")
        ratio = float(width_ratio) / float(height_ratio)
    except (ValueError, IndexError, ZeroDivisionError) as exc:
        raise EpisodeVideoError("成片比例无效") from exc
    width = math.sqrt(megapixels * 1_000_000 * ratio)
    height = width / ratio
    return (
        max(64, round(width / 2) * 2),
        max(64, round(height / 2) * 2),
    )


def _assemble_episode(
    episode: VideoEpisode,
    shots: list[VideoEpisodeShot],
) -> str:
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise EpisodeVideoError("缺少视频合成依赖 imageio-ffmpeg") from exc

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    clips: list[tuple[Path, float]] = []
    for shot in shots:
        for segment in json.loads(shot.segments_json or "[]"):
            path = settings.image_dir / segment["filepath"]
            if not path.is_file():
                raise EpisodeVideoError(f"镜头 {shot.shot_no} 的生成片段已丢失")
            clips.append((path, float(segment["target_duration"])))
    if not clips:
        raise EpisodeVideoError("没有可合成的视频片段")

    width, height = _output_size(episode.aspect_ratio, episode.megapixels)
    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "2"]
    for index, (clip_path, duration) in enumerate(clips):
        command.extend(["-i", str(clip_path)])
        length = f"{duration:.6f}"
        filter_parts.append(
            f"[{index}:v:0]setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps=24,format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={length},"
            f"trim=duration={length},setpts=PTS-STARTPTS[v{index}]"
        )
        if _has_audio_stream(ffmpeg, clip_path):
            audio_input = (
                f"[{index}:a:0]aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"apad,atrim=duration={length},asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            audio_input = (
                f"anullsrc=r=48000:cl=stereo,atrim=duration={length},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        filter_parts.append(audio_input)
        concat_inputs.append(f"[v{index}][a{index}]")
    filter_parts.append(
        f"{''.join(concat_inputs)}concat=n={len(clips)}:v=1:a=1[outv][outa]"
    )

    output_path = _episode_path(episode.id) / "final.mp4"
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    try:
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            deadline = time.monotonic() + 6 * 60 * 60
            while process.poll() is None:
                try:
                    _check_episode_cancelled(episode.id)
                except EpisodeCancelled:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise EpisodeVideoError("整集视频合成超过 6 小时，已停止")
                time.sleep(2)
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", errors="replace")
            return_code = process.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EpisodeVideoError("整集视频合成失败，请检查 FFmpeg 与服务器资源") from exc
    if return_code != 0:
        output_path.unlink(missing_ok=True)
        logger.error("整集视频合成失败 episode_id=%s: %s", episode.id, stderr[-2000:])
        raise EpisodeVideoError("整集视频合成失败，请检查生成片段格式与服务器资源")
    return _relative_path(output_path)


def _persist_failure(
    episode_id: int,
    shot_index: int | None,
    exc: Exception,
) -> None:
    db = SessionLocal()
    try:
        episode = db.get(VideoEpisode, episode_id)
        if episode is None or episode.status == "cancelled":
            return
        if isinstance(exc, ComfyError):
            message = classify_task_error(exc).message
        elif isinstance(exc, (RunningHubError, EpisodeVideoError)):
            message = str(exc)
        else:
            logger.exception("单集视频制作失败 episode_id=%s", episode_id, exc_info=exc)
            message = "单集制作失败，请查看服务日志。"
        episode.status = "failed"
        episode.error = message
        episode.progress_message = message
        episode.updated_at = datetime.utcnow()
        if shot_index is not None and shot_index < len(episode.shots):
            shot = episode.shots[shot_index]
            shot.status = "failed"
            shot.error = message
        db.commit()
    finally:
        db.close()


@celery_app.task(
    name="app.workers.video_episode_tasks.run_video_episode",
    bind=True,
    acks_late=True,
    time_limit=24 * 60 * 60,
    soft_time_limit=23 * 60 * 60,
)
def run_video_episode(self, episode_id: int):
    return _run_video_episode(episode_id)


def _run_video_episode(episode_id: int):
    """Generate the episode with backend-specific scheduling, then assemble it."""
    db = SessionLocal()
    current_shot_index: int | None = None
    runninghub_client: RunningHubClient | None = None
    runninghub_workflow_id = ""
    runninghub_selection: dict = {}
    try:
        episode = db.get(VideoEpisode, episode_id)
        if episode is None:
            return {"status": "not_found", "episode_id": episode_id}
        if episode.status in {"cancelled", "completed"}:
            return {"status": episode.status, "episode_id": episode_id}
        episode.status = "processing"
        episode.error = None
        episode.progress_message = "正在准备工作流"
        episode.updated_at = datetime.utcnow()
        db.commit()

        generation_backend = episode.generation_backend or "comfyui"
        if generation_backend not in {"comfyui", "runninghub"}:
            raise EpisodeVideoError("单集使用了不支持的生成后端")
        if generation_backend == "runninghub":
            config = db.get(RunningHubConfig, 1)
            if config is None:
                raise EpisodeVideoError("请先配置 RunningHub API Key")
            try:
                api_key = decrypt_secret(config.api_key_encrypted)
            except ValueError as exc:
                raise EpisodeVideoError("RunningHub API Key 解密失败") from exc
            workflow_config = json.loads(episode.workflow_config_json or "{}")
            runninghub_selection = workflow_config.get("runninghub") or {}
            runninghub_workflow_id = str(
                runninghub_selection.get("workflow_id") or ""
            )
            if not runninghub_workflow_id:
                raise EpisodeVideoError("当前单集没有配置 RunningHub 视频工作流")
            runninghub_client = RunningHubClient(api_key)
        client = ComfyClient() if generation_backend == "comfyui" else None
        db.commit()
        if generation_backend == "runninghub":
            if runninghub_client is None:
                raise EpisodeVideoError("RunningHub 客户端未初始化")
            _run_runninghub_episode_shots(
                episode_id,
                runninghub_client,
                runninghub_workflow_id,
                runninghub_selection,
            )
        for shot in (
            sorted(episode.shots, key=lambda item: item.shot_index)
            if generation_backend == "comfyui"
            else []
        ):
            db.expire_all()
            episode = db.get(VideoEpisode, episode_id)
            if episode is None:
                return {"status": "not_found", "episode_id": episode_id}
            if episode.status == "cancelled":
                raise EpisodeCancelled()
            shot = (
                db.query(VideoEpisodeShot)
                .filter(
                    VideoEpisodeShot.episode_id == episode_id,
                    VideoEpisodeShot.shot_index == shot.shot_index,
                )
                .first()
            )
            if shot is None or shot.status == "completed":
                continue
            current_shot_index = shot.shot_index
            data = json.loads(shot.data_json)
            planned_segments = duration_segments(float(data["duration_seconds"]))
            generated_segments = json.loads(shot.segments_json or "[]")
            shot.status = "processing"
            shot.error = None
            episode.current_shot_index = shot.shot_index
            episode.progress_message = (
                f"正在制作第 {shot.shot_index + 1}/{episode.total_shots} 镜"
            )
            db.commit()

            ref_images, ref_videos, ref_audios = _asset_references(episode, data)
            input_dir = settings.comfyui_input_path
            if (ref_images or ref_videos or ref_audios) and input_dir is None:
                raise EpisodeVideoError("未配置 ComfyUI 素材输入目录")
            for segment_index, (generation_duration, target_duration) in enumerate(
                planned_segments
            ):
                if segment_index < len(generated_segments):
                    previous_file = (
                        settings.image_dir
                        / generated_segments[segment_index]["filepath"]
                    )
                    if not previous_file.is_file():
                        raise EpisodeVideoError(f"镜头 {shot.shot_no} 的生成片段已丢失")
                    continue
                _check_episode_cancelled(episode_id)
                prompt = data.get("prompt") or data["visual_description"]
                videos_for_segment = list(ref_videos)
                if segment_index > 0:
                    previous = (
                        settings.image_dir
                        / generated_segments[segment_index - 1]["filepath"]
                    )
                    if generation_backend == "comfyui":
                        if input_dir is None:
                            raise EpisodeVideoError("未配置 ComfyUI 素材输入目录")
                        input_dir.mkdir(parents=True, exist_ok=True)
                        continuation_name = (
                            f"episode_{episode_id}_continue_{shot.shot_index}_"
                            f"{segment_index}{previous.suffix or '.mp4'}"
                        )
                        shutil.copyfile(previous, input_dir / continuation_name)
                        videos_for_segment = [continuation_name, *ref_videos[:2]]
                    prompt += (
                        "\nContinue the same shot from the supplied reference video's "
                        "final moment. Preserve the same characters, scene, lighting, "
                        "and camera motion."
                    )

                db.expire_all()
                episode = db.get(VideoEpisode, episode_id)
                if episode is None or episode.status == "cancelled":
                    raise EpisodeCancelled()
                episode.progress_message = (
                    f"正在生成第 {shot.shot_index + 1}/{episode.total_shots} 镜"
                    f" · 片段 {segment_index + 1}/{len(planned_segments)}"
                )
                db.commit()

                if generation_backend == "comfyui":
                    workflow = build_video_workflow(
                        prompt=prompt,
                        duration=generation_duration,
                        aspect_ratio=data["aspect_ratio"],
                        megapixels=float(data["megapixels"]),
                        seed=-1,
                        ref_images=ref_images,
                        ref_videos=videos_for_segment,
                        ref_audios=ref_audios,
                    )
                    prompt_id = None
                    if (
                        episode.current_shot_index == shot.shot_index
                        and episode.current_segment_index == segment_index
                    ):
                        prompt_id = episode.comfy_prompt_id
                    if not prompt_id:
                        prompt_id = client.submit_prompt(workflow)
                        episode.comfy_prompt_id = prompt_id
                        episode.current_shot_index = shot.shot_index
                        episode.current_segment_index = segment_index
                        db.commit()
                    media = client.wait_for_media(
                        prompt_id,
                        timeout=settings.comfy_timeout,
                        cancel_check=lambda: _check_episode_cancelled(episode_id),
                    )
                    videos = [item for item in media if item.get("kind") == "video"]
                    if not videos:
                        raise EpisodeVideoError(f"镜头 {shot.shot_no} 未生成视频")
                    meta = videos[0]
                    content = client.download_image(
                        meta["filename"],
                        meta.get("subfolder", ""),
                        meta.get("type", "output"),
                    )
                    relative_path = _save_clip(
                        episode_id,
                        shot.shot_index,
                        segment_index,
                        meta["filename"],
                        content,
                    )
                generated_segments.append(
                    {
                        "filepath": relative_path,
                        "target_duration": target_duration,
                    }
                )
                shot.segments_json = json.dumps(generated_segments, ensure_ascii=False)
                if generation_backend == "comfyui":
                    episode.comfy_prompt_id = None
                episode.current_segment_index = segment_index + 1
                db.commit()

            shot.status = "completed"
            shot.error = None
            episode.progress_message = (
                f"已完成第 {shot.shot_index + 1}/{episode.total_shots} 镜"
            )
            db.commit()

        db.expire_all()
        episode = db.get(VideoEpisode, episode_id)
        if episode is None or episode.status == "cancelled":
            raise EpisodeCancelled()
        shots = sorted(episode.shots, key=lambda item: item.shot_index)
        current_shot_index = None
        episode.progress_message = "镜头全部完成，正在合成整集视频"
        db.commit()
        output_filepath = _assemble_episode(episode, shots)
        _check_episode_cancelled(episode_id)
        episode.output_filepath = output_filepath
        episode.status = "completed"
        episode.error = None
        episode.progress_message = "整集视频制作完成"
        episode.comfy_prompt_id = None
        episode.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "completed", "episode_id": episode_id}
    except EpisodeCancelled:
        db.rollback()
        episode = db.get(VideoEpisode, episode_id)
        if episode is not None:
            (_episode_path(episode_id) / "final.mp4").unlink(missing_ok=True)
            episode.status = "cancelled"
            episode.progress_message = "单集制作已取消"
            episode.updated_at = datetime.utcnow()
            if current_shot_index is not None and current_shot_index < len(episode.shots):
                shot = episode.shots[current_shot_index]
                if shot.status == "processing":
                    shot.status = "cancelled"
            db.commit()
        return {"status": "cancelled", "episode_id": episode_id}
    except Exception as exc:
        db.rollback()
        _persist_failure(episode_id, current_shot_index, exc)
        return {
            "status": "failed",
            "episode_id": episode_id,
            "error": str(exc)[:500],
        }
    finally:
        if runninghub_client is not None:
            runninghub_client.close()
        db.close()


@celery_app.task(
    name="app.workers.video_episode_tasks.run_video_series",
    bind=True,
    acks_late=True,
    time_limit=30 * 24 * 60 * 60,
    soft_time_limit=29 * 24 * 60 * 60,
)
def run_video_series(self, series_id: int):
    """Run every confirmed episode in folder order, stopping at the first failure."""
    db = SessionLocal()
    try:
        series = db.get(VideoSeries, series_id)
        if series is None:
            return {"status": "not_found", "series_id": series_id}
        if series.status in {"cancelled", "completed"}:
            return {"status": series.status, "series_id": series_id}
        links = (
            db.query(VideoSeriesEpisode)
            .filter(VideoSeriesEpisode.series_id == series_id)
            .order_by(VideoSeriesEpisode.episode_index)
            .all()
        )
        if not links:
            raise EpisodeVideoError("整剧任务中没有可制作的分集")
        series.status = "processing"
        series.error = None
        series.progress_message = "正在准备整剧制作"
        series.updated_at = datetime.utcnow()
        db.commit()

        for index, link in enumerate(links):
            db.expire_all()
            series = db.get(VideoSeries, series_id)
            if series is None or series.status == "cancelled":
                return {"status": "cancelled", "series_id": series_id}
            episode = db.get(VideoEpisode, link.episode_id)
            if episode is None:
                raise EpisodeVideoError(f"第 {index + 1} 集记录已丢失")
            if episode.status == "completed":
                continue
            series.current_episode_index = index
            series.progress_message = f"正在制作第 {index + 1}/{len(links)} 集"
            series.updated_at = datetime.utcnow()
            db.commit()

            result = _run_video_episode(episode.id)
            db.expire_all()
            series = db.get(VideoSeries, series_id)
            if series is None or series.status == "cancelled":
                return {"status": "cancelled", "series_id": series_id}
            if result.get("status") != "completed":
                episode = db.get(VideoEpisode, link.episode_id)
                if result.get("status") == "cancelled":
                    series.status = "cancelled"
                    series.progress_message = "整剧制作已取消"
                    for remaining in links[index + 1 :]:
                        pending_episode = db.get(VideoEpisode, remaining.episode_id)
                        if pending_episode is not None and pending_episode.status == "queued":
                            pending_episode.status = "cancelled"
                            pending_episode.progress_message = "整剧制作已取消"
                    db.commit()
                    return {"status": "cancelled", "series_id": series_id}
                error = (
                    episode.error
                    if episode is not None and episode.error
                    else result.get("error", "分集制作失败")
                )
                series.status = "failed"
                series.error = error
                series.progress_message = f"第 {index + 1}/{len(links)} 集失败：{error}"
                series.updated_at = datetime.utcnow()
                for remaining in links[index + 1 :]:
                    pending_episode = db.get(VideoEpisode, remaining.episode_id)
                    if pending_episode is not None and pending_episode.status == "queued":
                        pending_episode.status = "draft"
                        pending_episode.progress_message = "等待整剧任务恢复"
                db.commit()
                return {
                    "status": "failed",
                    "series_id": series_id,
                    "episode_id": link.episode_id,
                    "error": error,
                }

        db.expire_all()
        series = db.get(VideoSeries, series_id)
        if series is None:
            return {"status": "not_found", "series_id": series_id}
        if series.status == "cancelled":
            return {"status": "cancelled", "series_id": series_id}
        series.status = "completed"
        series.error = None
        series.progress_message = f"全部 {len(links)} 集制作完成"
        series.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "completed", "series_id": series_id}
    except Exception as exc:
        db.rollback()
        logger.exception("整剧视频制作失败 series_id=%s", series_id)
        series = db.get(VideoSeries, series_id)
        if series is not None and series.status != "cancelled":
            series.status = "failed"
            series.error = str(exc)[:500]
            series.progress_message = "整剧制作失败，请查看服务日志"
            series.updated_at = datetime.utcnow()
            db.commit()
        return {
            "status": "failed",
            "series_id": series_id,
            "error": str(exc)[:500],
        }
    finally:
        db.close()

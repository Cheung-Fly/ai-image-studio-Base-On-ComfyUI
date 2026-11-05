"""Automatic episode-video import, review, execution, and downloads."""

import json
import logging
import mimetypes
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..database import get_db
from ..models import (
    CustomModelConfig,
    RunningHubConfig,
    User,
    VideoEpisode,
    VideoEpisodeShot,
    VideoSeries,
    VideoSeriesEpisode,
)
from ..schemas import (
    VideoEpisodeAssetOut,
    VideoEpisodeOut,
    VideoEpisodeShotOut,
    VideoEpisodeStartRequest,
    VideoEpisodeSummaryOut,
    VideoSeriesEpisodeOut,
    VideoSeriesOut,
    VideoSeriesStartRequest,
    VideoSeriesSummaryOut,
)
from ..security import decrypt_secret
from ..services.comfy_client import ComfyClient, ComfyError
from ..services.llm_client import LLMError
from ..services.runninghub import (
    RunningHubClient,
    RunningHubError,
    workflow_media_fields,
    workflow_text_fields,
)
from ..services.task_reliability import stop_comfy_prompt
from ..services.video_episode import (
    _MEDIA_EXTENSIONS,
    MAX_ARCHIVE_BYTES,
    MAX_SERIES_BYTES,
    MAX_SERIES_EPISODES,
    MAX_SERIES_FILES,
    EpisodePackage,
    EpisodePackageError,
    infer_asset_matches,
    ingest_episode_archive,
    ingest_episode_folder,
)
from ..services.video_workflow import VIDEO_WORKFLOW_FILE
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from ..workers.celery_app import celery_app
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/video-agent/episodes", tags=["video-agent"])
series_router = APIRouter(prefix="/video-agent/series", tags=["video-agent"])


def _custom_config(
    db: Session,
    user: User,
    provider: str,
    custom_config_id: int | None,
) -> dict[str, str] | None:
    if provider not in {"openai", "llama", "custom"}:
        raise HTTPException(422, "不支持的素材匹配模型")
    if provider != "custom":
        return None
    config = (
        db.get(CustomModelConfig, custom_config_id)
        if custom_config_id is not None
        else None
    )
    if config is None or config.user_id != user.id:
        raise HTTPException(400, "自定义模型配置无效")
    try:
        api_key = decrypt_secret(config.api_key_encrypted)
    except ValueError as exc:
        raise HTTPException(502, "自定义模型 API Key 解密失败") from exc
    return {"base_url": config.base_url, "api_key": api_key, "model": config.model}


def _episode_response(episode: VideoEpisode) -> VideoEpisodeOut:
    assets = json.loads(episode.assets_json)
    asset_outputs = [
        VideoEpisodeAssetOut(
            id=asset["id"],
            filename=asset["filename"],
            media_type=asset["media_type"],
            size=asset["size"],
            file_url=f"/api/video-agent/episodes/{episode.id}/assets/{asset['id']}",
        )
        for asset in assets
    ]
    shot_outputs = []
    for shot in episode.shots:
        data = json.loads(shot.data_json)
        segments = json.loads(shot.segments_json or "[]")
        shot_outputs.append(
            VideoEpisodeShotOut(
                shot_index=shot.shot_index,
                shot_no=shot.shot_no,
                framing=data.get("framing", ""),
                camera_movement=data.get("camera_movement", ""),
                visual_description=data.get("visual_description", ""),
                duration_seconds=float(data.get("duration_seconds", 5)),
                dialogue=data.get("dialogue", ""),
                sound_effects=data.get("sound_effects", ""),
                prompt=data.get("prompt", ""),
                asset_ids=data.get("asset_ids", []),
                suggested_asset_ids=data.get("suggested_asset_ids", []),
                match_confidence=float(data.get("match_confidence", 0)),
                match_source=data.get("match_source", "agent"),
                status=shot.status,
                segment_count=len(segments),
                error=shot.error,
            )
        )
    output_url = (
        f"/api/video-agent/episodes/{episode.id}/output"
        if episode.output_filepath
        else None
    )
    return VideoEpisodeOut(
        id=episode.id,
        title=episode.title,
        status=episode.status,
        assets=asset_outputs,
        shots=shot_outputs,
        aspect_ratio=episode.aspect_ratio,
        megapixels=episode.megapixels,
        generation_backend=episode.generation_backend,
        workflow_config=json.loads(episode.workflow_config_json or "{}"),
        current_shot_index=episode.current_shot_index,
        total_shots=episode.total_shots,
        progress_message=episode.progress_message,
        mapping_warning=episode.mapping_warning,
        error=episode.error,
        output_url=output_url,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
    )


def _get_episode(db: Session, user: User, episode_id: int) -> VideoEpisode:
    episode = (
        db.query(VideoEpisode)
        .filter(VideoEpisode.id == episode_id, VideoEpisode.user_id == user.id)
        .first()
    )
    if episode is None:
        raise HTTPException(404, "单集任务不存在")
    return episode


def _get_series(db: Session, user: User, series_id: int) -> VideoSeries:
    series = (
        db.query(VideoSeries)
        .filter(VideoSeries.id == series_id, VideoSeries.user_id == user.id)
        .first()
    )
    if series is None:
        raise HTTPException(404, "整剧任务不存在")
    return series


def _series_links(db: Session, series_id: int) -> list[VideoSeriesEpisode]:
    return (
        db.query(VideoSeriesEpisode)
        .filter(VideoSeriesEpisode.series_id == series_id)
        .order_by(VideoSeriesEpisode.episode_index)
        .all()
    )


def _series_response(series: VideoSeries, db: Session) -> VideoSeriesOut:
    links = _series_links(db, series.id)
    progress_message = series.progress_message
    if series.status == "processing" and links:
        index = min(series.current_episode_index, len(links) - 1)
        progress_message = (
            f"第 {index + 1}/{len(links)} 集："
            f"{links[index].episode.progress_message}"
        )
    return VideoSeriesOut(
        id=series.id,
        title=series.title,
        status=series.status,
        total_episodes=series.total_episodes,
        current_episode_index=series.current_episode_index,
        progress_message=progress_message,
        error=series.error,
        episodes=[
            VideoSeriesEpisodeOut(
                folder_name=link.folder_name,
                episode=_episode_response(link.episode),
            )
            for link in links
        ],
        created_at=series.created_at,
        updated_at=series.updated_at,
    )


def _episode_folder_sort_key(folder_name: str) -> tuple[tuple[int, int | str], ...]:
    parts = [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", folder_name)
    ]
    return tuple([*parts, (2, folder_name.casefold())])


def _is_supported_series_file(path: PurePosixPath) -> bool:
    suffix = path.suffix.casefold()
    return (
        suffix == ".xlsx"
        or (
            suffix == ".json"
            and path.stem.casefold() in {"storyboard", "分镜"}
        )
        or suffix.lstrip(".") in _MEDIA_EXTENSIONS
    )


def _remove_imported_assets(assets: list[dict]) -> None:
    input_dir = settings.comfyui_input_path
    if input_dir is None:
        return
    for asset in assets:
        try:
            (input_dir / asset["stored_filename"]).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "清理未提交的整剧素材失败 filename=%s",
                asset.get("stored_filename", ""),
            )


@router.post("", response_model=VideoEpisodeOut, status_code=201)
async def import_episode(
    file: UploadFile = File(...),
    provider: str = Form("openai"),
    custom_config_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import one ZIP package, parse its storyboard, and suggest asset matches."""
    if not file.filename or Path(file.filename).suffix.lower() != ".zip":
        raise HTTPException(400, "请上传包含分镜与素材的 .zip 文件")
    custom_config = _custom_config(db, user, provider, custom_config_id)
    check_rate_limit(user)
    check_and_increment_chat_quota(user, db)

    package_dir = settings.image_dir / "episode_packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    archive_path = package_dir / f"{uuid.uuid4().hex}.zip"
    package = None
    try:
        total = 0
        with archive_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise HTTPException(413, "单集素材 ZIP 最大支持 1 GB")
                destination.write(chunk)
        if total == 0:
            raise HTTPException(400, "上传的素材包为空")
        package = await run_in_threadpool(ingest_episode_archive, archive_path)
    except EpisodePackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        archive_path.unlink(missing_ok=True)
        await file.close()

    matches: dict[str, dict] = {}
    mapping_warning = None
    if package.assets:
        try:
            matches = await run_in_threadpool(
                infer_asset_matches,
                package.storyboard,
                package.assets,
                provider,
                custom_config,
            )
        except LLMError as exc:
            mapping_warning = "Agent 未能完成素材推断，请检查模型配置后手动匹配。"
            logger.warning("单集素材匹配失败: %s", exc)

    episode = VideoEpisode(
        user_id=user.id,
        title=package.storyboard.video_title,
        status="draft",
        storyboard_json=package.storyboard.model_dump_json(),
        assets_json=json.dumps(package.assets, ensure_ascii=False),
        mapping_warning=mapping_warning,
        total_shots=len(package.storyboard.shots),
    )
    db.add(episode)
    try:
        db.flush()
        for index, storyboard_shot in enumerate(package.storyboard.shots):
            suggestion = matches.get(storyboard_shot.shot_no, {})
            selected_ids = suggestion.get("asset_ids", [])
            data = storyboard_shot.model_dump(mode="json")
            data.update(
                {
                    "asset_ids": selected_ids,
                    "suggested_asset_ids": selected_ids,
                    "match_confidence": suggestion.get("confidence", 0.0),
                    "match_source": "agent" if suggestion else "none",
                }
            )
            db.add(
                VideoEpisodeShot(
                    episode_id=episode.id,
                    shot_index=index,
                    shot_no=storyboard_shot.shot_no,
                    data_json=json.dumps(data, ensure_ascii=False),
                )
            )
        db.commit()
        db.refresh(episode)
    except Exception:
        db.rollback()
        if package is not None:
            input_dir = settings.comfyui_input_path
            if input_dir is not None:
                for asset in package.assets:
                    (input_dir / asset["stored_filename"]).unlink(missing_ok=True)
        raise
    return _episode_response(episode)


@series_router.post("", response_model=VideoSeriesOut, status_code=201)
async def import_series(
    files: list[UploadFile] = File(...),
    provider: str = Form("openai"),
    custom_config_id: int | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Import a selected root folder containing one storyboard folder per episode."""
    if not files:
        raise HTTPException(400, "请选择包含各集子文件夹的整剧材料文件夹")
    if len(files) > MAX_SERIES_FILES:
        raise HTTPException(413, f"整剧文件夹最多包含 {MAX_SERIES_FILES} 个文件")
    declared_bytes = sum(upload.size or 0 for upload in files)
    if declared_bytes > MAX_SERIES_BYTES:
        raise HTTPException(413, "整剧材料总大小不能超过 4 GB")
    custom_config = _custom_config(db, user, provider, custom_config_id)
    check_rate_limit(user)
    check_and_increment_chat_quota(user, db)

    import_root = settings.image_dir / "series_imports" / uuid.uuid4().hex
    import_root.mkdir(parents=True, exist_ok=True)
    root_name: str | None = None
    episode_dirs: dict[str, tuple[str, Path]] = {}
    seen_paths: set[str] = set()
    total_bytes = 0
    imported_assets: list[dict] = []
    committed = False
    try:
        for upload in files:
            raw_name = (upload.filename or "").replace("\\", "/")
            raw_parts = raw_name.split("/")
            if (
                not raw_name
                or raw_name.startswith("/")
                or re.match(r"^[A-Za-z]:", raw_name)
                or any(
                    not part
                    or part in {".", ".."}
                    or len(part) > 255
                    or any(ord(char) < 32 for char in part)
                    for part in raw_parts
                )
                or len(raw_name) > 1024
            ):
                raise EpisodePackageError("整剧文件夹中包含不安全的相对路径")
            if root_name is None:
                root_name = raw_parts[0]
                if len(root_name) > 128:
                    raise EpisodePackageError("整剧文件夹名称不能超过 128 个字符")
            elif raw_parts[0] != root_name:
                raise EpisodePackageError("上传文件必须来自同一个整剧材料文件夹")

            relative = PurePosixPath(*raw_parts[2:]) if len(raw_parts) >= 3 else None
            supported = _is_supported_series_file(PurePosixPath(raw_parts[-1]))
            if relative is None and supported:
                raise EpisodePackageError("请将分镜和素材分别放入每集子文件夹")

            target: Path | None = None
            if relative is not None and supported:
                folder_name = raw_parts[1]
                folder_key = folder_name.casefold()
                existing = episode_dirs.get(folder_key)
                if existing and existing[0] != folder_name:
                    raise EpisodePackageError("分集文件夹名称不能只在大小写上不同")
                if not existing:
                    if len(episode_dirs) >= MAX_SERIES_EPISODES:
                        raise EpisodePackageError(
                            f"整剧最多支持 {MAX_SERIES_EPISODES} 个分集"
                        )
                    stage_dir = import_root / f"episode-{len(episode_dirs) + 1:03d}"
                    episode_dirs[folder_key] = (folder_name, stage_dir)
                else:
                    stage_dir = existing[1]

                relative_key = f"{folder_key}/{relative.as_posix()}".casefold()
                if relative_key in seen_paths:
                    raise EpisodePackageError("整剧文件夹中包含重复文件路径")
                seen_paths.add(relative_key)
                target = stage_dir / Path(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)

            if target is not None:
                with target.open("wb") as destination:
                    while chunk := await upload.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_SERIES_BYTES:
                            raise HTTPException(413, "整剧材料总大小不能超过 4 GB")
                        destination.write(chunk)
            else:
                while chunk := await upload.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_SERIES_BYTES:
                        raise HTTPException(413, "整剧材料总大小不能超过 4 GB")

        if not root_name or not episode_dirs:
            raise EpisodePackageError("所选文件夹中没有可识别的分集材料")

        staged_episodes: list[
            tuple[str, EpisodePackage, dict[str, dict], str | None]
        ] = []
        ordered_dirs = sorted(
            episode_dirs.values(), key=lambda item: _episode_folder_sort_key(item[0])
        )
        for folder_name, stage_dir in ordered_dirs:
            package = await run_in_threadpool(ingest_episode_folder, stage_dir)
            imported_assets.extend(package.assets)
            matches: dict[str, dict] = {}
            mapping_warning = None
            if package.assets:
                try:
                    matches = await run_in_threadpool(
                        infer_asset_matches,
                        package.storyboard,
                        package.assets,
                        provider,
                        custom_config,
                    )
                except LLMError as exc:
                    mapping_warning = (
                        "Agent 未能完成素材推断，请检查模型配置后手动匹配。"
                    )
                    logger.warning(
                        "整剧分集素材匹配失败 folder=%s: %s", folder_name, exc
                    )
            staged_episodes.append((folder_name, package, matches, mapping_warning))

        series = VideoSeries(
            user_id=user.id,
            title=root_name,
            status="draft",
            total_episodes=len(staged_episodes),
            progress_message="已导入，请逐集检查分镜与素材",
        )
        db.add(series)
        db.flush()
        for episode_index, (folder_name, package, matches, mapping_warning) in enumerate(
            staged_episodes
        ):
            episode = VideoEpisode(
                user_id=user.id,
                title=package.storyboard.video_title,
                status="draft",
                storyboard_json=package.storyboard.model_dump_json(),
                assets_json=json.dumps(package.assets, ensure_ascii=False),
                mapping_warning=mapping_warning,
                total_shots=len(package.storyboard.shots),
            )
            db.add(episode)
            db.flush()
            db.add(
                VideoSeriesEpisode(
                    series_id=series.id,
                    episode_id=episode.id,
                    episode_index=episode_index,
                    folder_name=folder_name,
                )
            )
            for shot_index, storyboard_shot in enumerate(package.storyboard.shots):
                suggestion = matches.get(storyboard_shot.shot_no, {})
                selected_ids = suggestion.get("asset_ids", [])
                data = storyboard_shot.model_dump(mode="json")
                data.update(
                    {
                        "asset_ids": selected_ids,
                        "suggested_asset_ids": selected_ids,
                        "match_confidence": suggestion.get("confidence", 0.0),
                        "match_source": "agent" if suggestion else "none",
                    }
                )
                db.add(
                    VideoEpisodeShot(
                        episode_id=episode.id,
                        shot_index=shot_index,
                        shot_no=storyboard_shot.shot_no,
                        data_json=json.dumps(data, ensure_ascii=False),
                    )
                )
        db.commit()
        committed = True
        db.refresh(series)
        return _series_response(series, db)
    except EpisodePackageError as exc:
        db.rollback()
        if not committed:
            _remove_imported_assets(imported_assets)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        if not committed:
            _remove_imported_assets(imported_assets)
        raise
    finally:
        for upload in files:
            await upload.close()
        try:
            shutil.rmtree(import_root)
        except OSError:
            logger.warning("清理整剧导入临时目录失败 path=%s", import_root)


@router.get("", response_model=list[VideoEpisodeSummaryOut])
def list_episodes(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 100:
        raise HTTPException(400, "分页参数无效")
    return (
        db.query(VideoEpisode)
        .filter(VideoEpisode.user_id == user.id)
        .order_by(VideoEpisode.id.desc())
        .limit(limit)
        .all()
    )


@router.get("/{episode_id}", response_model=VideoEpisodeOut)
def get_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _episode_response(_get_episode(db, user, episode_id))


@router.get("/{episode_id}/assets/{asset_id}")
def get_episode_asset(
    episode_id: int,
    asset_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = _get_episode(db, user, episode_id)
    asset = next(
        (item for item in json.loads(episode.assets_json) if item["id"] == asset_id),
        None,
    )
    if asset is None:
        raise HTTPException(404, "素材不存在")
    input_dir = settings.comfyui_input_path
    path = (input_dir / asset["stored_filename"]).resolve()
    if path.parent != input_dir.resolve() or not path.is_file():
        raise HTTPException(404, "素材文件已丢失")
    media_type = mimetypes.guess_type(asset["filename"])[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=asset["filename"])


def _validate_start_request(
    episode: VideoEpisode,
    request: VideoEpisodeStartRequest,
) -> dict[str, dict]:
    if len(request.shots) != episode.total_shots:
        raise HTTPException(422, "每个分镜都必须确认一次")
    selections = {shot.shot_no: shot for shot in request.shots}
    if len(selections) != len(request.shots):
        raise HTTPException(422, "镜号不能重复")
    stored_assets = {asset["id"]: asset for asset in json.loads(episode.assets_json)}
    by_shot_no = {shot.shot_no: shot for shot in episode.shots}
    if set(selections) != set(by_shot_no):
        raise HTTPException(422, "分镜列表已变化，请刷新后重试")

    validated = {}
    for shot_no, selection in selections.items():
        if len(set(selection.asset_ids)) != len(selection.asset_ids):
            raise HTTPException(422, f"镜头 {shot_no} 的素材不能重复")
        counts = {"image": 0, "video": 0, "audio": 0}
        for asset_id in selection.asset_ids:
            asset = stored_assets.get(asset_id)
            if asset is None:
                raise HTTPException(422, f"镜头 {shot_no} 引用了不属于本集的素材")
            counts[asset["media_type"]] += 1
        if counts["image"] > 9 or counts["video"] > 3 or counts["audio"] > 3:
            raise HTTPException(422, f"镜头 {shot_no} 超出工作流支持的素材数量")
        if counts["audio"] and not (counts["image"] or counts["video"]):
            raise HTTPException(422, f"镜头 {shot_no} 使用音频时还需要至少一张参考图或一个参考视频")
        validated[shot_no] = {
            "asset_ids": selection.asset_ids,
            "duration_seconds": selection.duration_seconds,
        }
    return validated


def _apply_workflow_selection(
    episode: VideoEpisode,
    request: VideoEpisodeStartRequest,
    db: Session,
    workflow_cache: dict[str, tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]]
    | None = None,
) -> None:
    if request.generation_backend == "comfyui":
        workflow_file = request.workflow_file or VIDEO_WORKFLOW_FILE
        if workflow_file != VIDEO_WORKFLOW_FILE:
            raise HTTPException(422, "所选 ComfyUI 视频工作流与自动视频素材管线不兼容")
        workflow_config = {"workflow_file": workflow_file}
    else:
        runninghub = db.get(RunningHubConfig, 1)
        if runninghub is None or not runninghub.api_key_encrypted:
            raise HTTPException(400, "请先配置 RunningHub API Key")
        if request.runninghub is None:
            raise HTTPException(422, "请先选择 RunningHub 视频工作流")
        try:
            api_key = decrypt_secret(runninghub.api_key_encrypted)
        except ValueError as exc:
            raise HTTPException(502, "RunningHub API Key 解密失败") from exc
        cached_workflow = (
            workflow_cache.get(request.runninghub.workflow_id)
            if workflow_cache is not None
            else None
        )
        if cached_workflow is None:
            client = RunningHubClient(api_key)
            try:
                workflow = client.get_workflow(request.runninghub.workflow_id)
            except RunningHubError as exc:
                raise HTTPException(422, str(exc)) from exc
            finally:
                client.close()
            cached_workflow = (
                workflow_text_fields(workflow),
                workflow_media_fields(workflow),
            )
            if workflow_cache is not None:
                workflow_cache[request.runninghub.workflow_id] = cached_workflow
        fields, media_fields = cached_workflow
        if not any(
            field["node_id"] == request.runninghub.prompt_node_id
            and field["field_name"] == request.runninghub.prompt_field
            for field in fields
        ):
            raise HTTPException(422, "所选正向提示词输入不属于该 RunningHub 工作流")
        if request.runninghub.negative_prompt_node_id and not any(
            field["node_id"] == request.runninghub.negative_prompt_node_id
            and field["field_name"] == request.runninghub.negative_prompt_field
            for field in fields
        ):
            raise HTTPException(422, "所选负向提示词输入不属于该 RunningHub 工作流")
        assets = {
            asset["id"]: asset for asset in json.loads(episode.assets_json)
        }
        for shot in episode.shots:
            data = json.loads(shot.data_json)
            counts = {"image": 0, "video": 0, "audio": 0}
            for asset_id in data.get("asset_ids", []):
                counts[assets[asset_id]["media_type"]] += 1
            for kind, count in counts.items():
                if count > len(media_fields[kind]):
                    label = {"image": "图片", "video": "视频", "audio": "音频"}[kind]
                    raise HTTPException(
                        422,
                        f"镜头 {shot.shot_no} 需要 {count} 个{label}输入，"
                        f"所选 RunningHub 工作流仅支持 {len(media_fields[kind])} 个",
                    )
        workflow_config = {
            "runninghub": request.runninghub.model_dump()
        }
    episode.generation_backend = request.generation_backend
    episode.workflow_config_json = json.dumps(workflow_config, ensure_ascii=False)


def _enqueue_episode(episode: VideoEpisode, db: Session) -> None:
    task_id = uuid.uuid4().hex
    episode.celery_task_id = task_id
    episode.status = "queued"
    episode.error = None
    episode.progress_message = "已排队，等待开始"
    episode.updated_at = datetime.utcnow()
    db.commit()
    try:
        celery_app.send_task(
            "app.workers.video_episode_tasks.run_video_episode",
            args=[episode.id],
            task_id=task_id,
        )
    except Exception as exc:
        episode.status = "failed"
        episode.error = "单集任务无法加入生成队列，请检查后台 worker 与 Redis。"
        episode.progress_message = "队列提交失败"
        db.commit()
        raise HTTPException(503, episode.error) from exc


@router.post("/{episode_id}/start", response_model=VideoEpisodeOut, status_code=202)
def start_episode(
    episode_id: int,
    request: VideoEpisodeStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = _get_episode(db, user, episode_id)
    if episode.status != "draft":
        raise HTTPException(409, "只有待确认的单集可以启动制作")
    _apply_episode_settings(episode, request, db)
    _enqueue_episode(episode, db)
    db.refresh(episode)
    return _episode_response(episode)


@router.post("/{episode_id}/resume", response_model=VideoEpisodeOut, status_code=202)
def resume_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = _get_episode(db, user, episode_id)
    if episode.status != "failed":
        raise HTTPException(409, "只有失败的单集可以继续制作")
    if episode.comfy_prompt_id:
        try:
            stop_comfy_prompt(ComfyClient(), episode.comfy_prompt_id)
        except ComfyError as exc:
            raise HTTPException(503, "无法确认 ComfyUI 中的旧任务已停止，暂不能安全重试") from exc
        episode.comfy_prompt_id = None
    failed_shot = next((shot for shot in episode.shots if shot.status == "failed"), None)
    if failed_shot is None and not all(shot.status == "completed" for shot in episode.shots):
        raise HTTPException(409, "没有可继续制作的失败镜头")
    if failed_shot is not None:
        failed_shot.status = "pending"
        failed_shot.error = None
    episode.output_filepath = None
    _enqueue_episode(episode, db)
    db.refresh(episode)
    return _episode_response(episode)


@router.post("/{episode_id}/cancel", response_model=VideoEpisodeOut)
def cancel_episode(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = _get_episode(db, user, episode_id)
    if episode.status not in {"queued", "processing"}:
        raise HTTPException(409, "只有排队或制作中的单集可以取消")
    task_id = episode.celery_task_id
    prompt_id = episode.comfy_prompt_id
    episode.status = "cancelled"
    episode.error = None
    episode.progress_message = "已请求取消"
    episode.updated_at = datetime.utcnow()
    db.commit()

    if task_id:
        try:
            celery_app.control.revoke(task_id, terminate=False)
        except Exception:
            logger.exception("撤销单集 Celery 任务失败 episode_id=%s", episode_id)
            episode.progress_message = "已标记取消，但无法通知后台 worker"
    if prompt_id:
        try:
            stop_comfy_prompt(ComfyClient(), prompt_id)
        except ComfyError:
            logger.exception("停止单集 ComfyUI prompt 失败 episode_id=%s", episode_id)
            episode.progress_message = "已标记取消，但无法确认 ComfyUI 已停止"
    db.commit()
    db.refresh(episode)
    return _episode_response(episode)


@router.get("/{episode_id}/output")
def download_episode_output(
    episode_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    episode = _get_episode(db, user, episode_id)
    if not episode.output_filepath:
        raise HTTPException(404, "整集视频尚未完成")
    path = (settings.image_dir / episode.output_filepath).resolve()
    root = settings.image_dir.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, "成片文件已丢失")
    safe_title = re.sub(r'[<>:"/\\|?*\r\n]', "_", episode.title).strip(" .") or "episode"
    return FileResponse(path, media_type="video/mp4", filename=f"{safe_title}.mp4")


def _enqueue_series(series: VideoSeries, db: Session) -> None:
    task_id = uuid.uuid4().hex
    series.celery_task_id = task_id
    series.status = "queued"
    series.error = None
    series.progress_message = "已排队，等待开始制作整剧"
    series.updated_at = datetime.utcnow()
    db.commit()
    try:
        celery_app.send_task(
            "app.workers.video_episode_tasks.run_video_series",
            args=[series.id],
            task_id=task_id,
        )
    except Exception as exc:
        series.status = "failed"
        series.error = "整剧任务无法加入生成队列，请检查后台 worker 与 Redis。"
        series.progress_message = "队列提交失败"
        for link in _series_links(db, series.id):
            if link.episode.status == "queued":
                link.episode.status = "draft"
                link.episode.error = None
        db.commit()
        raise HTTPException(503, series.error) from exc


def _apply_episode_settings(
    episode: VideoEpisode,
    request: VideoEpisodeStartRequest,
    db: Session,
    workflow_cache: dict[str, tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]]
    | None = None,
) -> None:
    selections = _validate_start_request(episode, request)
    for shot in episode.shots:
        data = json.loads(shot.data_json)
        selection = selections[shot.shot_no]
        data.update(
            {
                "asset_ids": selection["asset_ids"],
                "duration_seconds": selection["duration_seconds"],
                "aspect_ratio": request.aspect_ratio,
                "megapixels": request.megapixels,
            }
        )
        shot.data_json = json.dumps(data, ensure_ascii=False)
        shot.status = "pending"
        shot.error = None
        shot.segments_json = "[]"
    episode.aspect_ratio = request.aspect_ratio
    episode.megapixels = request.megapixels
    episode.current_shot_index = 0
    episode.current_segment_index = 0
    episode.output_filepath = None
    episode.comfy_prompt_id = None
    episode.error = None
    _apply_workflow_selection(episode, request, db, workflow_cache)


@series_router.get("", response_model=list[VideoSeriesSummaryOut])
def list_series(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if limit < 1 or limit > 100:
        raise HTTPException(400, "分页参数无效")
    return (
        db.query(VideoSeries)
        .filter(VideoSeries.user_id == user.id)
        .order_by(VideoSeries.id.desc())
        .limit(limit)
        .all()
    )


@series_router.get("/workflows")
def list_video_workflows(_user: User = Depends(get_current_user)):
    """List video templates that preserve the episode pipeline's media behavior."""
    return [{"file": VIDEO_WORKFLOW_FILE, "backend": "comfyui"}]


@series_router.get("/{series_id}", response_model=VideoSeriesOut)
def get_series(
    series_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _series_response(_get_series(db, user, series_id), db)


@series_router.post("/{series_id}/start", response_model=VideoSeriesOut, status_code=202)
def start_series(
    series_id: int,
    request: VideoSeriesStartRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    series = _get_series(db, user, series_id)
    if series.status != "draft":
        raise HTTPException(409, "只有待确认的整剧材料可以启动制作")
    links = _series_links(db, series.id)
    selections = {item.episode_id: item.settings for item in request.episodes}
    expected_ids = {link.episode_id for link in links}
    if len(selections) != len(request.episodes) or set(selections) != expected_ids:
        raise HTTPException(422, "每个分集都必须确认一次，且不能包含其他分集")
    workflow_cache: dict[
        str, tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]
    ] = {}
    for link in links:
        episode = _get_episode(db, user, link.episode_id)
        if episode.status != "draft":
            raise HTTPException(409, f"第 {link.episode_index + 1} 集当前不能启动")
        try:
            _apply_episode_settings(
                episode,
                selections[episode.id],
                db,
                workflow_cache,
            )
        except HTTPException as exc:
            raise HTTPException(
                exc.status_code,
                f"第 {link.episode_index + 1} 集：{exc.detail}",
            ) from exc
        episode.status = "queued"
        episode.progress_message = "等待整剧队列执行"

    series.current_episode_index = 0
    _enqueue_series(series, db)
    db.refresh(series)
    return _series_response(series, db)


@series_router.post("/{series_id}/resume", response_model=VideoSeriesOut, status_code=202)
def resume_series(
    series_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    series = _get_series(db, user, series_id)
    if series.status != "failed":
        raise HTTPException(409, "只有失败的整剧任务可以继续制作")
    links = _series_links(db, series.id)
    failed_link = next(
        (link for link in links if link.episode.status == "failed"),
        None,
    )
    if failed_link is not None:
        episode = failed_link.episode
        if episode.comfy_prompt_id:
            try:
                stop_comfy_prompt(ComfyClient(), episode.comfy_prompt_id)
            except ComfyError as exc:
                raise HTTPException(
                    503,
                    "无法确认 ComfyUI 中的旧任务已停止，暂不能安全重试",
                ) from exc
            episode.comfy_prompt_id = None
        failed_shot = next(
            (shot for shot in episode.shots if shot.status == "failed"),
            None,
        )
        if failed_shot is None and not all(
            shot.status == "completed" for shot in episode.shots
        ):
            raise HTTPException(409, "失败分集没有可继续制作的镜头")
        if failed_shot is not None:
            failed_shot.status = "pending"
            failed_shot.error = None
        episode.output_filepath = None
    for link in links:
        if link.episode.status != "completed":
            link.episode.status = "queued"
            link.episode.error = None
    _enqueue_series(series, db)
    db.refresh(series)
    return _series_response(series, db)


@series_router.post("/{series_id}/cancel", response_model=VideoSeriesOut)
def cancel_series(
    series_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    series = _get_series(db, user, series_id)
    if series.status not in {"queued", "processing"}:
        raise HTTPException(409, "只有排队或制作中的整剧可以取消")
    task_id = series.celery_task_id
    prompt_ids: list[str] = []
    for link in _series_links(db, series.id):
        episode = link.episode
        if episode.status in {"queued", "processing"}:
            if episode.comfy_prompt_id:
                prompt_ids.append(episode.comfy_prompt_id)
            episode.status = "cancelled"
            episode.error = None
            episode.progress_message = "整剧制作已取消"
    series.status = "cancelled"
    series.error = None
    series.progress_message = "整剧制作已取消"
    series.updated_at = datetime.utcnow()
    db.commit()

    if task_id:
        try:
            celery_app.control.revoke(task_id, terminate=False)
        except Exception:
            logger.exception("撤销整剧 Celery 任务失败 series_id=%s", series_id)
            series.progress_message = "已标记取消，但无法通知后台 worker"
    for prompt_id in prompt_ids:
        try:
            stop_comfy_prompt(ComfyClient(), prompt_id)
        except ComfyError:
            logger.exception("停止整剧 ComfyUI prompt 失败 series_id=%s", series_id)
            series.progress_message = "已标记取消，但无法确认 ComfyUI 已停止"
    db.commit()
    db.refresh(series)
    return _series_response(series, db)

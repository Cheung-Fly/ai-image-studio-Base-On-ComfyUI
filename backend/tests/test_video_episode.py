import asyncio
import json
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile

from app.api.video_agent import _episode_folder_sort_key, import_series
from app.database import Base
from app.models import (
    User,
    VideoEpisode,
    VideoEpisodeShot,
    VideoSeries,
    VideoSeriesEpisode,
)
from app.schemas import DirectorStoryboardOut
from app.services.llm_client import LLMError
from app.services.video_episode import (
    EpisodePackageError,
    duration_segments,
    infer_asset_matches,
    ingest_episode_archive,
    ingest_episode_folder,
    parse_series_storyboards,
)
from app.workers.video_episode_tasks import (
    _generate_runninghub_segment,
    _output_size,
    _run_runninghub_episode_shots,
    _runninghub_node_info,
    run_video_series,
)


def storyboard() -> DirectorStoryboardOut:
    return DirectorStoryboardOut.model_validate(
        {
            "video_title": "沙暴之后",
            "theme": "寻找避难所",
            "characters": "陆沉",
            "shots": [
                {
                    "shot_no": "1",
                    "framing": "全景",
                    "camera_movement": "缓慢横移",
                    "visual_description": "陆沉走过被黄沙覆盖的街道。",
                    "duration_seconds": 6,
                    "prompt": "A lone survivor crossing a sand-buried street.",
                },
                {
                    "shot_no": "2",
                    "framing": "近景",
                    "visual_description": "陆沉抬头看向远处。",
                    "duration_seconds": 4,
                    "prompt": "Close-up of the survivor looking into the distance.",
                },
            ],
        }
    )


def storyboard_xlsx(prompt: str = "A lone survivor crossing a sand-buried street.") -> bytes:
    import io

    workbook = Workbook()
    sheet = workbook.active
    sheet["E4"] = "沙暴之后"
    sheet["E5"] = "寻找避难所"
    for cell, value in {
        "B9": "镜号",
        "D9": "景别",
        "E9": "运镜",
        "G9": "画面内容",
        "K9": "时长",
        "L9": "台词",
        "P9": "音效",
        "T9": "提示词",
    }.items():
        sheet[cell] = value
    sheet["B10"] = "1"
    sheet["D10"] = "全景"
    sheet["E10"] = "缓慢横移"
    sheet["G10"] = "陆沉走过被黄沙覆盖的街道。"
    sheet["K10"] = 6
    sheet["T10"] = prompt
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def create_series_database(episode_count: int):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db:
        user = User(username="video-test", password_hash="test")
        db.add(user)
        db.flush()
        episodes = [
            VideoEpisode(
                user_id=user.id,
                title=f"第 {index + 1} 集",
                status="queued",
                storyboard_json="{}",
                assets_json="[]",
                total_shots=0,
            )
            for index in range(episode_count)
        ]
        db.add_all(episodes)
        db.flush()
        series = VideoSeries(
            user_id=user.id,
            title="测试整剧",
            status="queued",
            total_episodes=episode_count,
        )
        db.add(series)
        db.flush()
        for index, episode in enumerate(episodes):
            db.add(
                VideoSeriesEpisode(
                    series_id=series.id,
                    episode_id=episode.id,
                    episode_index=index,
                    folder_name=f"第{index + 1}集",
                )
            )
        db.commit()
        series_id = series.id
        episode_ids = [episode.id for episode in episodes]
    return engine, session_factory, series_id, episode_ids


class VideoEpisodeServiceTests(unittest.TestCase):
    def test_runninghub_video_mapping_uploads_references_and_settings(self):
        uploaded = []

        class FakeRunningHub:
            def get_workflow(self, _workflow_id):
                return {
                    "1": {
                        "class_type": "CLIPTextEncode",
                        "inputs": {"text": "old prompt"},
                    },
                    "2": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "old.png"},
                    },
                    "3": {
                        "class_type": "VideoSampler",
                        "inputs": {
                            "width": 640,
                            "height": 360,
                            "duration": 8,
                        },
                    },
                }

            def upload_media(self, path):
                uploaded.append(path.name)
                return f"input/{path.name}"

        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.png"
            reference.write_bytes(b"image")
            mapped = _runninghub_node_info(
                FakeRunningHub(),
                "video-workflow",
                {
                    "prompt_node_id": "1",
                    "prompt_field": "text",
                },
                "A close shot",
                [reference],
                [],
                [],
                "16:9 (Widescreen)",
                1.0,
                5,
            )

        self.assertEqual(uploaded, ["reference.png"])
        self.assertIn(
            {"nodeId": "1", "fieldName": "text", "fieldValue": "A close shot"},
            mapped,
        )
        self.assertIn(
            {"nodeId": "2", "fieldName": "image", "fieldValue": "input/reference.png"},
            mapped,
        )
        self.assertIn(
            {"nodeId": "3", "fieldName": "width", "fieldValue": 1334},
            mapped,
        )
        self.assertIn(
            {"nodeId": "3", "fieldName": "duration", "fieldValue": 5},
            mapped,
        )

    def test_runninghub_video_segment_persists_remote_task_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_dir = root / "media"
            engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(engine, expire_on_commit=False)
            with session_factory() as db:
                user = User(username="runninghub-task-id-test", password_hash="test")
                db.add(user)
                db.flush()
                episode = VideoEpisode(
                    user_id=user.id,
                    title="任务 ID 测试",
                    status="processing",
                    storyboard_json="{}",
                    assets_json="[]",
                )
                db.add(episode)
                db.flush()
                db.add(
                    VideoEpisodeShot(
                        episode_id=episode.id,
                        shot_index=0,
                        shot_no="1",
                        data_json="{}",
                    )
                )
                db.commit()
                episode_id = episode.id

                class FakeRunningHub:
                    def generate(
                        self,
                        _workflow_id,
                        _node_info_list,
                        output_dir,
                        *,
                        task_id_callback=None,
                        **_kwargs,
                    ):
                        if task_id_callback:
                            task_id_callback("rh-task-1")
                        output = Path(output_dir) / "output.mp4"
                        output.write_bytes(b"video")
                        return {
                            "files": [
                                {
                                    "kind": "video",
                                    "path": str(output),
                                    "filename": "output.mp4",
                                }
                            ]
                        }

                fake_client = FakeRunningHub()
                test_settings = SimpleNamespace(image_dir=media_dir)
                with (
                    patch("app.workers.video_episode_tasks.settings", test_settings),
                    patch(
                        "app.workers.video_episode_tasks._runninghub_node_info",
                        return_value=[],
                    ),
                ):
                    episode = db.get(VideoEpisode, episode_id)
                    relative_path = _generate_runninghub_segment(
                        db,
                        fake_client,
                        episode,
                        "workflow",
                        {},
                        "prompt",
                        [],
                        [],
                        [],
                        0,
                        0,
                        "16:9 (Widescreen)",
                        1.0,
                        5.0,
                    )

                shot = (
                    db.query(VideoEpisodeShot)
                    .filter(
                        VideoEpisodeShot.episode_id == episode_id,
                        VideoEpisodeShot.shot_index == 0,
                    )
                    .first()
                )
                self.assertEqual(
                    json.loads(shot.runninghub_tasks_json),
                    ["rh-task-1"],
                )
                self.assertTrue((media_dir / relative_path).is_file())
            engine.dispose()

    def test_runninghub_parallelizes_independent_shots_but_keeps_segments_ordered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media_dir = root / "media"
            engine = create_engine(
                f"sqlite:///{(root / 'episode.db').as_posix()}",
                connect_args={"check_same_thread": False},
            )
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(engine, expire_on_commit=False)
            with session_factory() as db:
                user = User(username="runninghub-parallel-test", password_hash="test")
                db.add(user)
                db.flush()
                episode = VideoEpisode(
                    user_id=user.id,
                    title="并行测试",
                    status="processing",
                    storyboard_json="{}",
                    assets_json="[]",
                    generation_backend="runninghub",
                    total_shots=3,
                )
                db.add(episode)
                db.flush()
                episode_id = episode.id
                for index, duration in enumerate((16, 4, 4)):
                    db.add(
                        VideoEpisodeShot(
                            episode_id=episode_id,
                            shot_index=index,
                            shot_no=str(index + 1),
                            data_json=json.dumps(
                                {
                                    "duration_seconds": duration,
                                    "visual_description": f"shot {index + 1}",
                                    "aspect_ratio": "16:9 (Widescreen)",
                                    "megapixels": 1.0,
                                    "asset_ids": [],
                                }
                            ),
                        )
                    )
                db.commit()

            lock = threading.Lock()
            both_workers_started = threading.Barrier(2)
            active = 0
            max_active = 0
            active_by_shot: dict[int, int] = {}
            max_active_by_shot: dict[int, int] = {}

            def fake_generate_segment(
                _db,
                _client,
                episode,
                _workflow_id,
                _selection,
                _prompt,
                _ref_images,
                _ref_videos,
                _ref_audios,
                shot_index,
                segment_index,
                _aspect_ratio,
                _megapixels,
                _duration_seconds,
            ):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    active_by_shot[shot_index] = active_by_shot.get(shot_index, 0) + 1
                    max_active_by_shot[shot_index] = max(
                        max_active_by_shot.get(shot_index, 0),
                        active_by_shot[shot_index],
                    )
                try:
                    if shot_index < 2 and segment_index == 0:
                        both_workers_started.wait(timeout=5)
                    time.sleep(0.02)
                    target = (
                        media_dir
                        / "episodes"
                        / str(episode.id)
                        / "clips"
                        / f"shot-{shot_index + 1:03d}-segment-{segment_index + 1:03d}.mp4"
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"clip")
                    return target.relative_to(media_dir).as_posix()
                finally:
                    with lock:
                        active -= 1
                        active_by_shot[shot_index] -= 1

            test_settings = SimpleNamespace(
                image_dir=media_dir,
                comfyui_input_path=None,
                runninghub_max_parallel_shots=2,
            )
            try:
                with (
                    patch(
                        "app.workers.video_episode_tasks.SessionLocal",
                        session_factory,
                    ),
                    patch("app.workers.video_episode_tasks.settings", test_settings),
                    patch(
                        "app.workers.video_episode_tasks._generate_runninghub_segment",
                        side_effect=fake_generate_segment,
                    ),
                ):
                    _run_runninghub_episode_shots(
                        episode_id,
                        object(),
                        "workflow",
                        {},
                    )

                with session_factory() as db:
                    episode = db.get(VideoEpisode, episode_id)
                    shots = sorted(episode.shots, key=lambda shot: shot.shot_index)
                    self.assertEqual([shot.status for shot in shots], ["completed"] * 3)
                    self.assertEqual(
                        [len(json.loads(shot.segments_json)) for shot in shots],
                        [2, 1, 1],
                    )
                self.assertEqual(max_active, 2)
                self.assertEqual(max(max_active_by_shot.values()), 1)
            finally:
                engine.dispose()

    def test_inherits_duration_for_decimal_shot_rows_with_merged_cells(self):
        workbook = load_workbook(BytesIO(storyboard_xlsx()))
        sheet = workbook.active
        sheet["B11"] = "1.1"
        sheet["G11"] = "A detail shot within the same scene."
        sheet.merge_cells("K10:K11")
        output = BytesIO()
        workbook.save(output)

        parsed = parse_series_storyboards(output.getvalue())

        self.assertEqual(
            [shot.duration_seconds for shot in parsed[0][1].shots],
            [6, 6],
        )

    def test_parse_series_storyboard_preserves_long_workflow_prompt(self):
        long_prompt = "subject_definitions: " + ("consistent visual details. " * 200)
        storyboards = parse_series_storyboards(storyboard_xlsx(long_prompt))
        self.assertEqual(len(storyboards), 1)
        self.assertEqual(storyboards[0][1].shots[0].prompt, long_prompt.strip())

    def test_duration_segments_adapt_short_and_long_shots(self):
        self.assertEqual(duration_segments(1), [(2.0, 1.0)])
        self.assertEqual(duration_segments(15), [(15.0, 15.0)])
        self.assertEqual(duration_segments(16), [(15.0, 15.0), (2.0, 1.0)])
        self.assertEqual(
            duration_segments(31),
            [(15.0, 15.0), (15.0, 15.0), (2.0, 1.0)],
        )

    def test_ingest_reads_storyboard_and_copies_safe_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "comfy-input"
            archive_path = root / "episode.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("分镜.xlsx", storyboard_xlsx())
                archive.writestr("assets/陆沉-角色.png", b"sample image")
            with patch(
                "app.services.video_episode.settings",
                SimpleNamespace(comfyui_input_path=input_dir),
            ):
                package = ingest_episode_archive(archive_path)

            self.assertEqual(package.storyboard.video_title, "沙暴之后")
            self.assertEqual(package.storyboard.shots[0].duration_seconds, 6)
            self.assertEqual(package.assets[0]["filename"], "陆沉-角色.png")
            self.assertEqual(package.assets[0]["media_type"], "image")
            self.assertEqual(
                (input_dir / package.assets[0]["stored_filename"]).read_bytes(),
                b"sample image",
            )

    def test_ingest_rejects_traversal_paths_and_deletes_partial_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "comfy-input"
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("storyboard.xlsx", storyboard_xlsx())
                archive.writestr("../escape.png", b"bad")
            with (
                patch(
                    "app.services.video_episode.settings",
                    SimpleNamespace(comfyui_input_path=input_dir),
                ),
                self.assertRaisesRegex(EpisodePackageError, "不安全"),
            ):
                ingest_episode_archive(archive_path)
            self.assertFalse(input_dir.exists())

    def test_ingest_episode_folder_reads_storyboard_and_nested_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            episode_dir = root / "第2集"
            (episode_dir / "分镜").mkdir(parents=True)
            (episode_dir / "素材" / "角色").mkdir(parents=True)
            (episode_dir / "分镜" / "制作表.xlsx").write_bytes(storyboard_xlsx())
            (episode_dir / "素材" / "角色" / "陆沉.png").write_bytes(b"image")
            (episode_dir / "素材" / "说明.txt").write_text("ignored", encoding="utf-8")
            input_dir = root / "comfy-input"
            with patch(
                "app.services.video_episode.settings",
                SimpleNamespace(comfyui_input_path=input_dir),
            ):
                package = ingest_episode_folder(episode_dir)

            self.assertEqual(package.storyboard.video_title, "沙暴之后")
            self.assertEqual(
                [asset["filename"] for asset in package.assets],
                ["素材/角色/陆沉.png"],
            )
            self.assertEqual(
                (input_dir / package.assets[0]["stored_filename"]).read_bytes(),
                b"image",
            )

    def test_ingest_episode_folder_cleans_partial_copies_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            episode_dir = root / "第1集"
            episode_dir.mkdir()
            (episode_dir / "分镜.xlsx").write_bytes(storyboard_xlsx())
            (episode_dir / "a.png").write_bytes(b"ok")
            (episode_dir / "b.png").write_bytes(b"too large")
            input_dir = root / "comfy-input"
            with (
                patch(
                    "app.services.video_episode.settings",
                    SimpleNamespace(comfyui_input_path=input_dir),
                ),
                patch.dict(
                    "app.services.video_episode._ASSET_LIMITS",
                    {"image": 4},
                ),
                self.assertRaisesRegex(EpisodePackageError, "超过"),
            ):
                ingest_episode_folder(episode_dir)
            self.assertEqual(list(input_dir.iterdir()), [])

    def test_ingest_episode_folder_rejects_duplicate_storyboards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            episode_dir = Path(temp_dir) / "第1集"
            episode_dir.mkdir()
            (episode_dir / "分镜.xlsx").write_bytes(storyboard_xlsx())
            (episode_dir / "storyboard.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(EpisodePackageError, "只能包含"):
                ingest_episode_folder(episode_dir)

    def test_episode_folder_sort_is_natural(self):
        self.assertEqual(
            sorted(["第10集", "第2集", "第1集"], key=_episode_folder_sort_key),
            ["第1集", "第2集", "第10集"],
        )

    def test_import_series_persists_episodes_in_natural_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            engine = create_engine(
                "sqlite://",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine, expire_on_commit=False)
            media_settings = SimpleNamespace(
                image_dir=root / "image-storage",
                comfyui_input_path=root / "comfy-input",
            )
            files = [
                UploadFile(
                    filename=f"整剧/{folder}/分镜.xlsx",
                    file=BytesIO(storyboard_xlsx()),
                )
                for folder in ("第10集", "第2集")
            ]
            try:
                with session_factory() as db:
                    user = User(username="series-import-test", password_hash="test")
                    db.add(user)
                    db.commit()
                    with (
                        patch("app.api.video_agent.settings", media_settings),
                        patch("app.services.video_episode.settings", media_settings),
                        patch("app.api.video_agent.check_rate_limit"),
                        patch("app.api.video_agent.check_and_increment_chat_quota"),
                    ):
                        imported = asyncio.run(
                            import_series(
                                files=files,
                                provider="openai",
                                custom_config_id=None,
                                user=user,
                                db=db,
                            )
                        )

                self.assertEqual(
                    [entry.folder_name for entry in imported.episodes],
                    ["第2集", "第10集"],
                )
                self.assertEqual(imported.total_episodes, 2)
            finally:
                engine.dispose()

    @patch("app.services.video_episode.chat")
    def test_inference_returns_only_known_asset_ids_and_confidence(self, chat_mock):
        chat_mock.return_value = json.dumps(
            {
                "matches": [
                    {
                        "shot_no": "1",
                        "asset_ids": ["a0001", "unknown"],
                        "confidence": 0.84,
                    }
                ]
            }
        )
        with self.assertRaisesRegex(LLMError, "不存在"):
            infer_asset_matches(
                storyboard(),
                [{"id": "a0001", "filename": "陆沉.png", "media_type": "image"}],
                "openai",
            )

    @patch("app.services.video_episode.chat")
    def test_inference_uses_storyboard_text_and_keeps_model_suggestions(self, chat_mock):
        chat_mock.return_value = json.dumps(
            {
                "matches": [
                    {
                        "shot_no": "1",
                        "asset_ids": ["a0001"],
                        "confidence": 0.84,
                    }
                ]
            }
        )
        result = infer_asset_matches(
            storyboard(),
            [{"id": "a0001", "filename": "陆沉-角色.png", "media_type": "image"}],
            "openai",
        )
        self.assertEqual(result["1"]["asset_ids"], ["a0001"])
        self.assertEqual(result["1"]["confidence"], 0.84)
        self.assertIn("被黄沙覆盖", chat_mock.call_args.args[0])

    def test_output_size_keeps_selected_ratio_and_pixel_scale(self):
        width, height = _output_size("16:9 (Widescreen)", 1.0)
        self.assertEqual((width, height), (1334, 750))

    def test_series_worker_runs_episodes_in_order(self):
        engine, session_factory, series_id, episode_ids = create_series_database(2)
        processed = []

        def finish_episode(episode_id):
            processed.append(episode_id)
            with session_factory() as db:
                episode = db.get(VideoEpisode, episode_id)
                episode.status = "completed"
                db.commit()
            return {"status": "completed", "episode_id": episode_id}

        try:
            with (
                patch("app.workers.video_episode_tasks.SessionLocal", session_factory),
                patch(
                    "app.workers.video_episode_tasks._run_video_episode",
                    side_effect=finish_episode,
                ),
            ):
                result = run_video_series.run(series_id)

            self.assertEqual(processed, episode_ids)
            self.assertEqual(result["status"], "completed")
            with session_factory() as db:
                self.assertEqual(db.get(VideoSeries, series_id).status, "completed")
        finally:
            engine.dispose()

    def test_series_worker_stops_at_failure_and_releases_later_episodes(self):
        engine, session_factory, series_id, episode_ids = create_series_database(3)
        processed = []

        def fail_first_episode(episode_id):
            processed.append(episode_id)
            with session_factory() as db:
                episode = db.get(VideoEpisode, episode_id)
                episode.status = "failed"
                episode.error = "ComfyUI generation failed"
                db.commit()
            return {
                "status": "failed",
                "episode_id": episode_id,
                "error": "ComfyUI generation failed",
            }

        try:
            with (
                patch("app.workers.video_episode_tasks.SessionLocal", session_factory),
                patch(
                    "app.workers.video_episode_tasks._run_video_episode",
                    side_effect=fail_first_episode,
                ),
            ):
                result = run_video_series.run(series_id)

            self.assertEqual(processed, [episode_ids[0]])
            self.assertEqual(result["status"], "failed")
            with session_factory() as db:
                self.assertEqual(db.get(VideoSeries, series_id).status, "failed")
                self.assertEqual(db.get(VideoEpisode, episode_ids[1]).status, "draft")
                self.assertEqual(db.get(VideoEpisode, episode_ids[2]).status, "draft")
        finally:
            engine.dispose()

    def test_series_worker_cancels_later_episodes_when_current_episode_is_cancelled(self):
        engine, session_factory, series_id, episode_ids = create_series_database(2)
        processed = []

        def cancel_episode(episode_id):
            processed.append(episode_id)
            with session_factory() as db:
                episode = db.get(VideoEpisode, episode_id)
                episode.status = "cancelled"
                db.commit()
            return {"status": "cancelled", "episode_id": episode_id}

        try:
            with (
                patch("app.workers.video_episode_tasks.SessionLocal", session_factory),
                patch(
                    "app.workers.video_episode_tasks._run_video_episode",
                    side_effect=cancel_episode,
                ),
            ):
                result = run_video_series.run(series_id)

            self.assertEqual(processed, [episode_ids[0]])
            self.assertEqual(result["status"], "cancelled")
            with session_factory() as db:
                self.assertEqual(db.get(VideoSeries, series_id).status, "cancelled")
                self.assertEqual(db.get(VideoEpisode, episode_ids[1]).status, "cancelled")
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()

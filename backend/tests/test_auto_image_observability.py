"""Automatic-image planning progress tests."""

import json
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.auth import get_current_user
from app.api.auto_image import router as auto_image_router
from app.database import Base, get_db
from app.models import (
    AutoImageAsset,
    AutoImageBatch,
    AutoImageEvent,
    GenerationTask,
    ImageAsset,
    TaskEvent,
    TaskStatus,
    User,
)
from app.schemas import DirectorStoryboardOut, DirectorStoryboardShot
from app.services.llm_client import LLMError
from app.services.task_reliability import TaskDispatchError
from app.services.video_episode import ingest_episode_folder
from app.workers.auto_image_tasks import _safe_plan_error, plan_auto_image_batch


class AutoImageObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.sessions() as db:
            user = User(username="auto-image-test", password_hash="disabled")
            db.add(user)
            db.flush()
            batch = AutoImageBatch(
                user_id=user.id,
                title="test",
                source_filename="test.xlsx",
                episode_count=1,
                selected_episode_index=1,
                status="planning",
                storyboards_json=json.dumps(
                    [
                        {
                            "sheet_name": "Episode 1",
                            "storyboard": {
                                "video_title": "Test episode",
                                "shots": [
                                    {
                                        "shot_no": "1",
                                        "visual_description": "A character enters a room",
                                        "duration_seconds": 3,
                                    }
                                ],
                            },
                        }
                    ]
                ),
            )
            db.add(batch)
            db.commit()
            self.batch_id = batch.id
            self.user_id = user.id

    def tearDown(self):
        self.engine.dispose()

    def test_custom_model_credentials_are_redacted_from_error_logs(self):
        error = Exception(
            "Unable to connect to https://private.test/api/: secret-key"
        )
        safe_message = _safe_plan_error(
            error,
            {
                "api_key": "secret-key",
                "base_url": "https://private.test/api",
                "model": "model",
            },
        )

        self.assertNotIn("secret-key", safe_message)
        self.assertNotIn("https://private.test/api", safe_message)
        self.assertIn("[redacted]", safe_message)

        response_error = _safe_plan_error(
            Exception("LLM 返回 500: sensitive provider response"),
            None,
        )
        self.assertNotIn("sensitive provider response", response_error)
        self.assertIn("响应正文已省略", response_error)

    def create_client(self):
        app = FastAPI()
        app.include_router(auto_image_router, prefix="/api")

        def override_get_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: User(id=self.user_id)
        return TestClient(app)

    def test_plan_endpoint_returns_while_llm_planning_is_queued(self):
        storyboard = DirectorStoryboardOut(
            video_title="Test episode",
            shots=[
                DirectorStoryboardShot(
                    shot_no="1",
                    visual_description="A character enters a room",
                    duration_seconds=3,
                )
            ],
        )
        with (
            patch("app.api.auto_image.parse_series_storyboards", return_value=[
                ("Episode 1", storyboard)
            ]),
            patch("app.api.auto_image.check_rate_limit"),
            patch("app.api.auto_image.check_and_increment_chat_quota"),
            patch("app.api.auto_image.plan_auto_image_batch.delay") as enqueue,
            self.create_client() as client,
        ):
            response = client.post(
                "/api/auto-image/plan",
                data={"episode_index": "1", "provider": "openai"},
                files={"file": ("test.xlsx", b"workbook", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "planning")
        self.assertIn(
            "planning_queued",
            [event["event_type"] for event in response.json()["logs"]],
        )
        enqueue.assert_called_once_with(response.json()["id"], "openai", None)

    def test_plan_enqueue_failure_is_visible_and_persisted(self):
        storyboard = DirectorStoryboardOut(
            video_title="Test episode",
            shots=[
                DirectorStoryboardShot(
                    shot_no="1",
                    visual_description="A character enters a room",
                    duration_seconds=3,
                )
            ],
        )
        with (
            patch("app.api.auto_image.parse_series_storyboards", return_value=[
                ("Episode 1", storyboard)
            ]),
            patch("app.api.auto_image.check_rate_limit"),
            patch("app.api.auto_image.check_and_increment_chat_quota"),
            patch(
                "app.api.auto_image.plan_auto_image_batch.delay",
                side_effect=RuntimeError("broker unavailable"),
            ),
            self.create_client() as client,
        ):
            response = client.post(
                "/api/auto-image/plan",
                data={"episode_index": "1", "provider": "openai"},
                files={"file": ("test.xlsx", b"workbook", "application/octet-stream")},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "planning_failed")
        queue_failure = response.json()["logs"][-1]
        self.assertEqual(queue_failure["event_type"], "planning_queue_failed")
        self.assertEqual(queue_failure["level"], "error")

    def test_planning_success_persists_assets_and_timed_events(self):
        planned_assets = [
            {
                "asset_key": "character:hero",
                "category": "character",
                "name": "主角",
                "episodes": [1],
                "prompt": "Full-body character reference",
            }
        ]
        with (
            patch("app.workers.auto_image_tasks.SessionLocal", self.sessions),
            patch(
                "app.workers.auto_image_tasks.plan_series_assets",
                return_value=planned_assets,
            ),
        ):
            result = plan_auto_image_batch.run(self.batch_id, "openai")

        with self.sessions() as db:
            batch = db.get(AutoImageBatch, self.batch_id)
            events = (
                db.query(AutoImageEvent)
                .filter(AutoImageEvent.batch_id == self.batch_id)
                .order_by(AutoImageEvent.id)
                .all()
            )
            assets = (
                db.query(AutoImageAsset)
                .filter(AutoImageAsset.batch_id == self.batch_id)
                .all()
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(batch.status, "draft")
        self.assertEqual(len(assets), 1)
        self.assertIn("llm_request_started", [event.event_type for event in events])
        completed = next(
            event for event in events if event.event_type == "asset_plan_completed"
        )
        self.assertIsNotNone(completed.duration_ms)

    def test_llm_timeout_is_persisted_as_a_failed_batch_event(self):
        timeout = LLMError("The read operation timed out")
        with (
            patch("app.workers.auto_image_tasks.SessionLocal", self.sessions),
            patch(
                "app.workers.auto_image_tasks.plan_series_assets",
                side_effect=timeout,
            ),
        ):
            result = plan_auto_image_batch.run(self.batch_id, "openai")

        with self.sessions() as db:
            batch = db.get(AutoImageBatch, self.batch_id)
            events = (
                db.query(AutoImageEvent)
                .filter(AutoImageEvent.batch_id == self.batch_id)
                .order_by(AutoImageEvent.id)
                .all()
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(batch.status, "planning_failed")
        self.assertIn("The read operation timed out", events[-1].message)
        self.assertEqual(events[-1].level, "error")
        self.assertEqual(events[-2].event_type, "llm_request_failed")

    def create_asset_task(self, status, *, manual_retry_count=0, error=None):
        with self.sessions() as db:
            batch = db.get(AutoImageBatch, self.batch_id)
            batch.status = "draft"
            task = GenerationTask(
                user_id=self.user_id,
                prompt="Reference image prompt",
                status=status,
                manual_retry_count=manual_retry_count,
                error=error,
                error_code="comfy_unavailable" if error else None,
                comfy_prompt_id="old-comfy-prompt",
                runninghub_task_id="old-runninghub-task",
                celery_task_id="old-celery-task",
            )
            db.add(task)
            db.flush()
            asset = AutoImageAsset(
                batch_id=self.batch_id,
                asset_key=f"asset:{task.id}",
                category="character",
                name=f"Asset {task.id}",
                episode_indices_json="[1]",
                prompt=task.prompt,
                task_id=task.id,
            )
            db.add(asset)
            db.commit()
            return asset.id, task.id

    def test_failed_asset_error_is_visible_and_blocks_package_download(self):
        asset_id, task_id = self.create_asset_task(
            TaskStatus.failed,
            error="无法连接 ComfyUI，请检查服务状态与地址。",
        )

        with self.create_client() as client:
            batch_response = client.get(f"/api/auto-image/batches/{self.batch_id}")
            package_response = client.get(
                f"/api/auto-image/batches/{self.batch_id}/episodes/1.zip"
            )

        self.assertEqual(batch_response.status_code, 200)
        asset = next(
            item for item in batch_response.json()["assets"] if item["id"] == asset_id
        )
        self.assertEqual(asset["task_id"], task_id)
        self.assertEqual(asset["error_code"], "comfy_unavailable")
        self.assertIn("无法连接 ComfyUI", asset["error"])
        self.assertTrue(asset["retry_available"])
        self.assertEqual(package_response.status_code, 409)

    def test_episode_storyboard_endpoint_preserves_shot_order(self):
        with self.create_client() as client:
            response = client.get(
                f"/api/auto-image/batches/{self.batch_id}/episodes/1/storyboard"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [shot["shot_no"] for shot in response.json()["shots"]],
            ["1"],
        )

    def test_series_package_blocks_until_all_episode_assets_complete(self):
        self.create_asset_task(
            TaskStatus.failed,
            error="设定图仍未完成",
        )
        with self.create_client() as client:
            response = client.get(
                f"/api/auto-image/batches/{self.batch_id}/series.zip"
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("尚未完成", response.json()["detail"])

    def test_series_package_blocks_when_a_later_episode_asset_is_incomplete(self):
        asset_id, _ = self.create_asset_task(
            TaskStatus.failed,
            error="第二集设定图仍未完成",
        )
        with self.sessions() as db:
            batch = db.get(AutoImageBatch, self.batch_id)
            batch.episode_count = 2
            batch.storyboards_json = json.dumps(
                [
                    json.loads(batch.storyboards_json)[0],
                    {
                        "sheet_name": "Episode 2",
                        "storyboard": {
                            "video_title": "Test episode 2",
                            "shots": [
                                {
                                    "shot_no": "1",
                                    "visual_description": "A second episode shot",
                                    "duration_seconds": 4,
                                }
                            ],
                        },
                    },
                ]
            )
            db.get(AutoImageAsset, asset_id).episode_indices_json = "[2]"
            db.commit()

        with self.create_client() as client:
            response = client.get(
                f"/api/auto-image/batches/{self.batch_id}/series.zip"
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("第 2 集", response.json()["detail"])

    def test_series_package_uses_windows_safe_root_and_utf8_filename(self):
        with self.sessions() as db:
            batch = db.get(AutoImageBatch, self.batch_id)
            batch.title = "CON"
            batch.status = "completed"
            db.commit()

        with self.create_client() as client:
            response = client.get(
                f"/api/auto-image/batches/{self.batch_id}/series.zip"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-disposition"],
            "attachment; filename*=UTF-8''_CON-Agent3.zip",
        )
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            self.assertTrue(
                all(name.startswith("_CON/") for name in archive.namelist())
            )

    def test_series_package_is_ordered_and_importable_by_agent3(self):
        asset_id, task_id = self.create_asset_task(TaskStatus.completed)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            image_dir = temp_root / "images"
            image_dir.mkdir()
            (image_dir / "reference.png").write_bytes(b"reference-image")
            import_dir = temp_root / "comfy-input"

            first_storyboard = json.loads(
                json.dumps(
                    {
                        "video_title": "Episode One",
                        "theme": "A test story",
                        "shots": [
                            {
                                "shot_no": "2",
                                "visual_description": "Second storyboard row",
                                "duration_seconds": 4,
                            },
                            {
                                "shot_no": "1",
                                "visual_description": "First storyboard row",
                                "duration_seconds": 3,
                            },
                        ],
                    }
                )
            )
            second_storyboard = {
                "video_title": "Episode Two",
                "shots": [
                    {
                        "shot_no": "1",
                        "visual_description": "Second episode opening",
                        "duration_seconds": 5,
                    }
                ],
            }
            with self.sessions() as db:
                batch = db.get(AutoImageBatch, self.batch_id)
                batch.title = "测试剧集"
                batch.episode_count = 2
                batch.storyboards_json = json.dumps(
                    [
                        {"sheet_name": "Episode One", "storyboard": first_storyboard},
                        {"sheet_name": "Episode Two", "storyboard": second_storyboard},
                    ],
                    ensure_ascii=False,
                )
                db.get(AutoImageAsset, asset_id).episode_indices_json = "[1,2]"
                db.add(
                    ImageAsset(
                        task_id=task_id,
                        filename="reference.png",
                        filepath="reference.png",
                        media_type="image",
                    )
                )
                db.commit()

            with (
                patch(
                    "app.api.auto_image.settings",
                    SimpleNamespace(image_dir=image_dir),
                ),
                self.create_client() as client,
            ):
                response = client.get(
                    f"/api/auto-image/batches/{self.batch_id}/series.zip"
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["content-disposition"],
                "attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95%E5%89%A7%E9%9B%86-Agent3.zip",
            )
            extracted = temp_root / "extracted"
            with zipfile.ZipFile(BytesIO(response.content)) as archive:
                names = archive.namelist()
                archive.extractall(extracted)

            series_root = extracted / "测试剧集"
            episode_dirs = sorted(path for path in series_root.iterdir() if path.is_dir())
            self.assertEqual(
                [path.name for path in episode_dirs],
                ["01-Episode One", "02-Episode Two"],
            )
            delivery_manifest = json.loads(
                (series_root / "delivery_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [episode["episode_index"] for episode in delivery_manifest["episodes"]],
                [1, 2],
            )
            self.assertEqual(
                [shot["shot_no"] for shot in delivery_manifest["episodes"][0]["shot_order"]],
                ["2", "1"],
            )
            self.assertTrue(
                any("/assets/01-characters/" in name for name in names)
            )

            with patch(
                "app.services.video_episode.settings",
                SimpleNamespace(comfyui_input_path=import_dir),
            ):
                imported = [
                    ingest_episode_folder(episode_dir) for episode_dir in episode_dirs
                ]

        self.assertEqual(
            [shot.shot_no for shot in imported[0].storyboard.shots],
            ["2", "1"],
        )
        self.assertEqual(imported[0].assets[0]["media_type"], "image")
        self.assertEqual(imported[1].storyboard.video_title, "Episode Two")
        self.assertEqual(len(imported[1].assets), 1)

    def test_retry_requeues_only_failed_asset_and_completed_retry_unlocks_package(self):
        failed_asset_id, failed_task_id = self.create_asset_task(
            TaskStatus.failed,
            error="无法连接 ComfyUI，请检查服务状态与地址。",
        )
        completed_asset_id, completed_task_id = self.create_asset_task(
            TaskStatus.completed,
        )
        with self.sessions() as db:
            completed_task = db.get(GenerationTask, completed_task_id)
            completed_task.comfy_prompt_id = None
            db.add(
                TaskEvent(
                    task_id=failed_task_id,
                    event_type="generation_failed",
                    status="failed",
                    message="原始失败事件",
                )
            )
            db.commit()

        with tempfile.TemporaryDirectory() as image_dir:
            image_path = Path(image_dir) / "completed.png"
            image_path.write_bytes(b"completed-image")
            (Path(image_dir) / "retried.png").write_bytes(b"retried-image")
            with self.sessions() as db:
                db.add(
                    ImageAsset(
                        task_id=completed_task_id,
                        filename="completed.png",
                        filepath="completed.png",
                        media_type="image",
                    )
                )
                db.commit()

            request = {
                "episode_index": 1,
                "generation_backend": "comfyui",
                "workflow_file": "",
                "aspect_ratio": "3:2 (Photo)",
                "megapixels": 1,
                "seed": -1,
                "steps": 8,
                "refine_steps": 4,
            }
            with (
                patch(
                    "app.api.auto_image.settings",
                    SimpleNamespace(image_dir=Path(image_dir)),
                ),
                patch("app.api.auto_image.dispatch_generation") as dispatch,
                self.create_client() as client,
            ):
                retry_response = client.post(
                    (
                        f"/api/auto-image/batches/{self.batch_id}/assets/"
                        f"{failed_asset_id}/retry"
                    ),
                    json=request,
                )
                blocked_package = client.get(
                    f"/api/auto-image/batches/{self.batch_id}/episodes/1.zip"
                )

                with self.sessions() as db:
                    retried_task = db.get(GenerationTask, failed_task_id)
                    self.assertEqual(retried_task.status, TaskStatus.pending)
                    self.assertEqual(retried_task.manual_retry_count, 1)
                    self.assertIsNone(retried_task.comfy_prompt_id)
                    self.assertIsNone(retried_task.runninghub_task_id)
                    self.assertIsNone(retried_task.celery_task_id)
                    retried_task.status = TaskStatus.completed
                    retried_task.error = None
                    retried_task.error_code = None
                    retried_task.comfy_prompt_id = None
                    db.add(
                        ImageAsset(
                            task_id=failed_task_id,
                            filename="retried.png",
                            filepath="retried.png",
                            media_type="image",
                        )
                    )
                    db.commit()

                ready_package = client.get(
                    f"/api/auto-image/batches/{self.batch_id}/episodes/1.zip"
                )

        self.assertEqual(retry_response.status_code, 202)
        retry_body = retry_response.json()
        retried_asset = next(
            item for item in retry_body["assets"] if item["id"] == failed_asset_id
        )
        retained_asset = next(
            item for item in retry_body["assets"] if item["id"] == completed_asset_id
        )
        self.assertEqual(retried_asset["status"], "pending")
        self.assertEqual(retained_asset["status"], "completed")
        self.assertEqual(retained_asset["task_id"], completed_task_id)
        dispatch.assert_called_once()
        self.assertEqual(blocked_package.status_code, 409)
        self.assertEqual(ready_package.status_code, 200)
        with zipfile.ZipFile(BytesIO(ready_package.content)) as archive:
            names = archive.namelist()
            image_names = [name for name in names if name.endswith(".png")]
            manifest_name = next(
                name for name in names if name.endswith("/assets_manifest.json")
            )
            manifest = json.loads(archive.read(manifest_name))
        self.assertEqual(len(image_names), 2)
        self.assertEqual(len(manifest), 2)
        with self.sessions() as db:
            event_types = [
                event.event_type
                for event in db.query(TaskEvent)
                .filter(TaskEvent.task_id == failed_task_id)
                .order_by(TaskEvent.id)
            ]
        self.assertIn("generation_failed", event_types)
        self.assertIn("manual_retry_requested", event_types)

    def test_retry_rejects_non_failed_tasks_and_retry_limit(self):
        pending_asset_id, _ = self.create_asset_task(TaskStatus.pending)
        exhausted_asset_id, _ = self.create_asset_task(
            TaskStatus.failed,
            manual_retry_count=3,
            error="仍然失败",
        )
        request = {
            "episode_index": 1,
            "generation_backend": "comfyui",
            "aspect_ratio": "3:2 (Photo)",
            "megapixels": 1,
            "seed": -1,
            "steps": 8,
            "refine_steps": 4,
        }

        with (
            patch("app.api.auto_image.dispatch_generation") as dispatch,
            self.create_client() as client,
        ):
            pending_response = client.post(
                (
                    f"/api/auto-image/batches/{self.batch_id}/assets/"
                    f"{pending_asset_id}/retry"
                ),
                json=request,
            )
            exhausted_response = client.post(
                (
                    f"/api/auto-image/batches/{self.batch_id}/assets/"
                    f"{exhausted_asset_id}/retry"
                ),
                json=request,
            )

        self.assertEqual(pending_response.status_code, 409)
        self.assertEqual(exhausted_response.status_code, 409)
        dispatch.assert_not_called()

    def test_comfyui_retry_enqueue_failure_is_returned_as_failed_asset(self):
        asset_id, task_id = self.create_asset_task(
            TaskStatus.failed,
            error="上次生成失败",
        )
        request = {
            "episode_index": 1,
            "generation_backend": "comfyui",
            "aspect_ratio": "3:2 (Photo)",
            "megapixels": 1,
            "seed": -1,
            "steps": 8,
            "refine_steps": 4,
        }

        def fail_dispatch(db, task):
            task.status = TaskStatus.failed
            task.error_code = "queue_unavailable"
            task.error = "任务无法加入生成队列，请检查后台 worker 与 Redis。"
            db.commit()
            raise TaskDispatchError(task.error)

        with (
            patch(
                "app.api.auto_image.dispatch_generation",
                side_effect=fail_dispatch,
            ),
            self.create_client() as client,
        ):
            response = client.post(
                (
                    f"/api/auto-image/batches/{self.batch_id}/assets/"
                    f"{asset_id}/retry"
                ),
                json=request,
            )

        self.assertEqual(response.status_code, 202)
        asset = next(
            item for item in response.json()["assets"] if item["id"] == asset_id
        )
        self.assertEqual(asset["task_id"], task_id)
        self.assertEqual(asset["status"], "failed")
        self.assertEqual(asset["error_code"], "queue_unavailable")
        self.assertTrue(asset["retry_available"])
        self.assertIn("无法加入生成队列", asset["error"])
        self.assertIn(
            "asset_generation_retry_enqueue_failed",
            [event["event_type"] for event in response.json()["logs"]],
        )


if __name__ == "__main__":
    unittest.main()

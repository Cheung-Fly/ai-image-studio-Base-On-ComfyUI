"""Automatic-image planning progress tests."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.auth import get_current_user
from app.api.auto_image import router as auto_image_router
from app.database import Base
from app.database import get_db
from app.models import AutoImageAsset, AutoImageBatch, AutoImageEvent, User
from app.schemas import DirectorStoryboardOut, DirectorStoryboardShot
from app.services.llm_client import LLMError
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


if __name__ == "__main__":
    unittest.main()

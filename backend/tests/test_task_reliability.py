"""Task lifecycle, durable events, bounded retry, and cancellation tests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import database
from app.api.auth import get_current_user
from app.api.tasks import router as tasks_router
from app.database import Base, get_db
from app.models import GenerationTask, TaskEvent, TaskStatus, User
from app.services.comfy_client import ComfyError
from app.services.task_reliability import (
    TaskDispatchError,
    classify_task_error,
    dispatch_generation,
)
from app.services.workflow import WorkflowError
from app.workers.tasks import run_generation


class TaskReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.sessions() as db:
            self.user = User(username="local", password_hash="disabled")
            db.add(self.user)
            db.commit()
            db.refresh(self.user)
            self.user_id = self.user.id

        def override_get_db():
            with self.sessions() as db:
                yield db

        self.app = FastAPI()
        self.app.include_router(tasks_router, prefix="/api")
        self.app.dependency_overrides[get_db] = override_get_db
        self.app.dependency_overrides[get_current_user] = lambda: User(id=self.user_id)

    def tearDown(self):
        self.engine.dispose()

    def create_task(
        self,
        *,
        status=TaskStatus.pending,
        error_code=None,
        celery_task_id=None,
        comfy_prompt_id=None,
        manual_retry_count=0,
    ):
        with self.sessions() as db:
            task = GenerationTask(
                user_id=self.user_id,
                prompt="test prompt",
                status=status,
                error_code=error_code,
                celery_task_id=celery_task_id,
                comfy_prompt_id=comfy_prompt_id,
                manual_retry_count=manual_retry_count,
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            return task.id

    def test_event_history_is_persisted_and_returned_in_order(self):
        task_id = self.create_task(status=TaskStatus.processing)
        with self.sessions() as db:
            db.add_all(
                [
                    TaskEvent(
                        task_id=task_id,
                        event_type="started",
                        status="processing",
                        message="started",
                        attempt=1,
                    ),
                    TaskEvent(
                        task_id=task_id,
                        event_type="submitted",
                        status="processing",
                        message="submitted",
                        attempt=1,
                    ),
                ]
            )
            db.commit()

        with TestClient(self.app) as client:
            response = client.get(f"/api/tasks/{task_id}/events")

        self.assertEqual(response.status_code, 200)
        event_types = [event["event_type"] for event in response.json()]
        self.assertEqual(event_types, ["started", "submitted"])

    def test_manual_retry_is_safe_and_capped_at_three(self):
        task_id = self.create_task(
            status=TaskStatus.failed,
            error_code="comfy_unavailable",
        )
        with (
            patch.object(run_generation, "apply_async", return_value=Mock()) as enqueue,
            TestClient(self.app) as client,
        ):
            for expected_count in range(1, 4):
                if expected_count > 1:
                    with self.sessions() as db:
                        task = db.get(GenerationTask, task_id)
                        task.status = TaskStatus.failed
                        task.error_code = "comfy_unavailable"
                        task.error = "Unable to connect"
                        task.celery_task_id = None
                        db.commit()
                response = client.post(f"/api/tasks/{task_id}/retry")
                self.assertEqual(response.status_code, 202)
                self.assertEqual(
                    response.json()["manual_retry_count"],
                    expected_count,
                )

            with self.sessions() as db:
                task = db.get(GenerationTask, task_id)
                task.status = TaskStatus.failed
                task.error_code = "comfy_unavailable"
                db.commit()
            capped = client.post(f"/api/tasks/{task_id}/retry")

        self.assertEqual(capped.status_code, 409)
        self.assertEqual(enqueue.call_count, 3)
        with self.sessions() as db:
            events = (
                db.query(TaskEvent)
                .filter(TaskEvent.task_id == task_id)
                .order_by(TaskEvent.id.asc())
                .all()
            )
        self.assertEqual(
            sum(event.event_type == "manual_retry_requested" for event in events),
            3,
        )
        self.assertEqual(
            [
                event.attempt
                for event in events
                if event.event_type == "manual_retry_requested"
            ],
            [2, 3, 4],
        )

    def test_manual_timeout_retry_requires_a_persisted_prompt(self):
        resumable_id = self.create_task(
            status=TaskStatus.failed,
            error_code="comfy_timeout",
            comfy_prompt_id="prompt-existing",
        )
        ambiguous_id = self.create_task(
            status=TaskStatus.failed,
            error_code="comfy_timeout",
        )
        with (
            patch.object(run_generation, "apply_async", return_value=Mock()) as enqueue,
            TestClient(self.app) as client,
        ):
            resumable = client.post(f"/api/tasks/{resumable_id}/retry")
            ambiguous = client.post(f"/api/tasks/{ambiguous_id}/retry")

        self.assertEqual(resumable.status_code, 202)
        self.assertEqual(resumable.json()["comfy_prompt_id"], "prompt-existing")
        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(enqueue.call_count, 1)

    def test_cancel_marks_task_and_interrupts_only_its_running_prompt(self):
        task_id = self.create_task(
            status=TaskStatus.processing,
            celery_task_id="celery-1",
            comfy_prompt_id="prompt-1",
        )
        comfy = Mock()
        comfy.queue_info.return_value = {
            "queue_running": [[1, "prompt-1", {}, {}, []]],
            "queue_pending": [],
        }
        with (
            patch("app.api.tasks.celery_app.control.revoke") as revoke,
            patch("app.api.tasks.ComfyClient", return_value=comfy),
            TestClient(self.app) as client,
        ):
            response = client.post(f"/api/tasks/{task_id}/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"]["status"], "cancelled")
        self.assertEqual(response.json()["comfy_stop"], "interrupted")
        revoke.assert_called_once_with("celery-1", terminate=False)
        comfy.interrupt.assert_called_once()
        with self.sessions() as db:
            task = db.get(GenerationTask, task_id)
            event_types = [event.event_type for event in task.events]
        self.assertEqual(task.status, TaskStatus.cancelled)
        self.assertIn("cancel_requested", event_types)
        self.assertIn("comfy_stop_requested", event_types)

    def test_cancel_removes_matching_queued_prompt_without_interrupting_other_work(self):
        task_id = self.create_task(
            status=TaskStatus.processing,
            comfy_prompt_id="prompt-queued",
        )
        comfy = Mock()
        comfy.queue_info.return_value = {
            "queue_running": [[1, "other-running-prompt", {}, {}, []]],
            "queue_pending": [[2, "prompt-queued", {}, {}, []]],
        }
        with (
            patch("app.api.tasks.ComfyClient", return_value=comfy),
            TestClient(self.app) as client,
        ):
            response = client.post(f"/api/tasks/{task_id}/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comfy_stop"], "removed_from_queue")
        comfy.delete_queued.assert_called_once_with(["prompt-queued"])
        comfy.interrupt.assert_not_called()

    def test_connection_errors_are_classified_as_retryable(self):
        try:
            raise httpx.ConnectError("connection refused")
        except httpx.ConnectError as cause:
            error = ComfyError("Unable to connect")
            error.__cause__ = cause

        safe = classify_task_error(error)
        workflow = classify_task_error(WorkflowError("invalid"))

        self.assertEqual(safe.code, "comfy_unavailable")
        self.assertTrue(safe.retryable)
        self.assertEqual(workflow.code, "workflow_invalid")
        self.assertFalse(workflow.retryable)

    def test_worker_retries_poll_timeout_by_resuming_submitted_prompt(self):
        task_id = self.create_task(
            status=TaskStatus.processing,
            comfy_prompt_id="existing-prompt",
        )
        comfy = Mock()
        comfy.wait_for_media.side_effect = ComfyError("等待 ComfyUI 任务超时")

        with (
            patch("app.workers.tasks.SessionLocal", self.sessions),
            patch("app.workers.tasks.ComfyClient", return_value=comfy),
            patch.object(run_generation, "retry", side_effect=RuntimeError("retry scheduled")),
            self.assertRaisesRegex(RuntimeError, "retry scheduled"),
        ):
            run_generation.apply(args=(task_id,), throw=True)

        with self.sessions() as db:
            task = db.get(GenerationTask, task_id)
            event_types = [event.event_type for event in task.events]
        self.assertEqual(task.status, TaskStatus.pending)
        self.assertEqual(task.retry_count, 1)
        self.assertEqual(task.error_code, "comfy_timeout")
        self.assertIn("retry_scheduled", event_types)
        comfy.submit_prompt.assert_not_called()

    def test_queue_dispatch_failure_is_persisted_as_a_task_event(self):
        task_id = self.create_task()
        with (
            patch.object(
                run_generation,
                "apply_async",
                side_effect=RuntimeError("broker down"),
            ),
            self.sessions() as db,
        ):
            task = db.get(GenerationTask, task_id)
            with self.assertRaises(TaskDispatchError):
                dispatch_generation(db, task)

        with self.sessions() as db:
            task = db.get(GenerationTask, task_id)
            events = [event.event_type for event in task.events]
        self.assertEqual(task.status, TaskStatus.failed)
        self.assertEqual(task.error_code, "queue_unavailable")
        self.assertEqual(events, ["queued", "enqueue_failed"])

    def test_existing_sqlite_task_table_receives_new_reliability_columns(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE images (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE video_episode_shots (id INTEGER PRIMARY KEY)"
            )
        Base.metadata.create_all(bind=engine)

        with (
            patch.object(database, "engine", engine),
            patch.object(database, "_database_url", "sqlite://"),
        ):
            database._migrate_missing_columns()

        columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
        self.assertTrue(
            {
                "error_code",
                "runninghub_task_id",
                "celery_task_id",
                "retry_count",
                "manual_retry_count",
            }.issubset(columns)
        )
        shot_columns = {
            column["name"]
            for column in inspect(engine).get_columns("video_episode_shots")
        }
        self.assertIn("runninghub_tasks_json", shot_columns)
        engine.dispose()

    def test_worker_resumes_existing_prompt_without_submitting_duplicate(self):
        task_id = self.create_task(
            status=TaskStatus.processing,
            comfy_prompt_id="existing-prompt",
        )
        comfy = Mock()
        comfy.wait_for_media.return_value = []
        comfy.submit_prompt.side_effect = AssertionError("prompt must not be submitted twice")

        with (
            patch("app.workers.tasks.SessionLocal", self.sessions),
            patch("app.workers.tasks.ComfyClient", return_value=comfy),
        ):
            result = run_generation.apply(args=(task_id,), throw=True).result

        self.assertEqual(result["status"], "completed")
        comfy.submit_prompt.assert_not_called()
        comfy.wait_for_media.assert_called_once()
        with self.sessions() as db:
            task = db.get(GenerationTask, task_id)
            event_types = [event.event_type for event in task.events]
        self.assertEqual(task.status, TaskStatus.completed)
        self.assertIn("resumed", event_types)

    def test_worker_observes_cancellation_during_comfyui_poll(self):
        task_id = self.create_task(status=TaskStatus.pending)
        comfy = Mock()
        comfy.submit_prompt.return_value = "prompt-cancel"

        def cancel_during_poll(_prompt_id, cancel_check):
            with self.sessions() as db:
                task = db.get(GenerationTask, task_id)
                task.status = TaskStatus.cancelled
                db.commit()
            cancel_check()

        comfy.wait_for_media.side_effect = cancel_during_poll
        with (
            patch("app.workers.tasks.SessionLocal", self.sessions),
            patch("app.workers.tasks.ComfyClient", return_value=comfy),
            patch("app.workers.tasks.build_workflow", return_value={}),
        ):
            result = run_generation.apply(args=(task_id,), throw=True).result

        self.assertEqual(result["status"], "cancelled")
        with self.sessions() as db:
            task = db.get(GenerationTask, task_id)
            self.assertEqual(task.status, TaskStatus.cancelled)


if __name__ == "__main__":
    unittest.main()

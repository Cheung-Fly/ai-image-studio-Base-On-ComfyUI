"""Persistence, ownership, and request resolution for director skills."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.auth import get_current_user
from app.api.director import router as director_router
from app.database import Base, get_db
from app.models import DirectorSkill, User
from app.schemas import DirectorStoryboardOut, DirectorStoryboardShot


class DirectorSkillApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.sessions = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        with self.sessions() as db:
            db.add_all(
                [
                    User(id=1, username="director-one", password_hash="disabled"),
                    User(id=2, username="director-two", password_hash="disabled"),
                ]
            )
            db.commit()
        self.current_user_id = 1
        self.app = FastAPI()
        self.app.include_router(director_router, prefix="/api")

        def override_get_db():
            with self.sessions() as db:
                yield db

        self.app.dependency_overrides[get_db] = override_get_db
        self.app.dependency_overrides[get_current_user] = lambda: User(
            id=self.current_user_id
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_skill_crud_is_scoped_to_current_user(self):
        created = self.client.post(
            "/api/director/skills",
            json={
                "name": " 悬疑节奏 ",
                "instructions": " 每个关键动作后留出反应镜头。 ",
            },
        )
        self.assertEqual(created.status_code, 201)
        skill = created.json()
        self.assertEqual(skill["name"], "悬疑节奏")
        self.assertEqual(skill["instructions"], "每个关键动作后留出反应镜头。")

        listed = self.client.get("/api/director/skills")
        self.assertEqual([entry["id"] for entry in listed.json()], [skill["id"]])

        updated = self.client.put(
            f"/api/director/skills/{skill['id']}",
            json={"name": "慢镜头", "instructions": "关键情绪处使用慢镜头。"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "慢镜头")

        self.current_user_id = 2
        self.assertEqual(self.client.get("/api/director/skills").json(), [])
        self.assertEqual(
            self.client.delete(f"/api/director/skills/{skill['id']}").status_code,
            404,
        )
        self.current_user_id = 1
        self.assertEqual(
            self.client.delete(f"/api/director/skills/{skill['id']}").status_code,
            200,
        )

    def test_import_skill_markdown_from_folder(self):
        markdown = (
            "---\n"
            'name: "悬疑镜头"\n'
            "description: Keep visual clues consistent.\n"
            "---\n"
            "\n"
            "Use reaction shots after important discoveries."
        )
        response = self.client.post(
            "/api/director/skills/import",
            files={
                "file": (
                    "mystery-camera/SKILL.md",
                    markdown.encode("utf-8"),
                    "text/markdown",
                )
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "悬疑镜头")
        self.assertIn("Keep visual clues consistent.", response.json()["instructions"])
        self.assertIn(
            "Use reaction shots after important discoveries.",
            response.json()["instructions"],
        )

    def test_skill_import_rejects_non_skill_files(self):
        response = self.client.post(
            "/api/director/skills/import",
            files={"file": ("README.md", b"not a skill", "text/markdown")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "所选文件夹中必须包含 SKILL.md")

    def test_storyboard_resolves_selected_skills_for_current_user(self):
        with self.sessions() as db:
            skill = DirectorSkill(
                user_id=1,
                name="悬疑节奏",
                instructions="每个关键动作后留出反应镜头。",
            )
            db.add(skill)
            db.commit()
            db.refresh(skill)
            skill_id = skill.id

        with (
            patch("app.api.director.check_rate_limit"),
            patch("app.api.director.check_and_increment_chat_quota"),
            patch(
                "app.api.director.generate_storyboard",
                return_value=DirectorStoryboardOut(
                    video_title="Test",
                    shots=[
                        DirectorStoryboardShot(
                            shot_no="1",
                            visual_description="A character enters a room.",
                            duration_seconds=3,
                        )
                    ],
                ),
            ) as generate,
        ):
            response = self.client.post(
                "/api/director/storyboard",
                json={
                    "script": "A character enters a room.",
                    "provider": "openai",
                    "skill_ids": [skill_id],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            generate.call_args.args[4],
            [("悬疑节奏", "每个关键动作后留出反应镜头。")],
        )

    def test_storyboard_rejects_another_users_skill(self):
        with self.sessions() as db:
            skill = DirectorSkill(
                user_id=2,
                name="Private",
                instructions="Private instruction",
            )
            db.add(skill)
            db.commit()
            db.refresh(skill)
            skill_id = skill.id

        with (
            patch("app.api.director.check_rate_limit"),
            patch("app.api.director.generate_storyboard") as generate,
        ):
            response = self.client.post(
                "/api/director/storyboard",
                json={
                    "script": "A character enters a room.",
                    "provider": "openai",
                    "skill_ids": [skill_id],
                },
            )

        self.assertEqual(response.status_code, 400)
        generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

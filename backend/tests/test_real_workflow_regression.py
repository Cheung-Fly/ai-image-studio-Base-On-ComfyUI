"""Opt-in real service regression tests; they are skipped by default.

Run from backend/ with AISTUDIO_COMFYUI_INTEGRATION=1 or
AISTUDIO_RUNNINGHUB_INTEGRATION=1. RunningHub submits a paid remote task.
"""

import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.config import settings
from app.services.comfy_client import ComfyClient
from app.services.workflow import build_workflow


class RealComfyUIWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("AISTUDIO_COMFYUI_INTEGRATION") == "1",
        "set AISTUDIO_COMFYUI_INTEGRATION=1 to submit a local ComfyUI generation",
    )
    def test_default_workflow_generates_a_valid_image(self):
        client = ComfyClient(timeout=settings.comfy_timeout)
        queue = client.queue_info()
        if queue.get("queue_running") or queue.get("queue_pending"):
            self.skipTest("ComfyUI has queued work; refusing to interfere with it")

        workflow = build_workflow(
            prompt="A small red apple centered on a plain light background.",
            negative_prompt="",
            aspect_ratio="1:1 (Square)",
            megapixels=0.25,
            seed=20261106,
            steps=2,
            refine_steps=1,
        )
        prompt_id = client.submit_prompt(workflow)
        images = client.wait_for_images(prompt_id)
        self.assertTrue(images)

        image = client.download_image(
            images[0]["filename"],
            images[0].get("subfolder", ""),
            images[0].get("type", "output"),
        )
        with Image.open(BytesIO(image)) as decoded:
            decoded.verify()


class RealRunningHubWorkflowTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("AISTUDIO_RUNNINGHUB_INTEGRATION") == "1",
        "set AISTUDIO_RUNNINGHUB_INTEGRATION=1 to submit one paid RunningHub generation",
    )
    def test_saved_runninghub_workflow_returns_media(self):
        from app.database import SessionLocal
        from app.models import RunningHubConfig
        from app.security import decrypt_secret
        from app.services.runninghub import RunningHubClient

        with SessionLocal() as db:
            config = db.get(RunningHubConfig, 1)
            self.assertIsNotNone(config, "No saved RunningHub workflow configuration exists.")
            api_key = decrypt_secret(config.api_key_encrypted)
            workflow_id = config.workflow_id
            prompt_mapping = {
                "nodeId": config.prompt_node_id,
                "fieldName": config.prompt_field,
                "fieldValue": "A small red apple centered on a plain light background.",
            }
            node_info_list = [prompt_mapping]
            if config.negative_prompt_node_id and config.negative_prompt_field:
                node_info_list.append(
                    {
                        "nodeId": config.negative_prompt_node_id,
                        "fieldName": config.negative_prompt_field,
                        "fieldValue": "",
                    }
                )

        client = RunningHubClient(api_key)
        try:
            with tempfile.TemporaryDirectory(prefix="aistudio-runninghub-regression-") as directory:
                result = client.generate(
                    workflow_id,
                    node_info_list,
                    Path(directory),
                )
                self.assertTrue(result["task_id"])
                self.assertTrue(result["files"])
                for media in result["files"]:
                    output = Path(media["path"])
                    self.assertTrue(output.is_file())
                    self.assertGreater(output.stat().st_size, 0)
                    if media["kind"] == "image":
                        with Image.open(output) as decoded:
                            decoded.verify()
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()

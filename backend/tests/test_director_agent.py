import json
import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from app.schemas import DirectorStoryboardOut
from app.services.director_agent import (
    DIRECTOR_SYSTEM_PROMPT,
    DirectorOutputError,
    export_storyboard_xlsx,
    generate_storyboard,
    parse_storyboard_response,
)
from app.services.llm_client import chat


def sample_storyboard() -> dict:
    return {
        "video_title": "第一集·沙暴",
        "theme": "灾变后寻找避难所",
        "shooting_time": "",
        "deadline": "",
        "props": "旧电线",
        "characters": "陆沉",
        "shots": [
            {
                "shot_no": "1",
                "framing": "全景",
                "camera_movement": "缓慢横移",
                "visual_description": "沙暴掠过废弃街道，陆沉独自走入画面。",
                "duration_seconds": 6,
                "dialogue": "陆沉（OS）：“灾难发生后的第七天。”",
                "sound_effects": "风沙呼啸，远处金属碰撞",
                "prompt": (
                    "Wide shot, a lone survivor crossing a sand-buried city street, "
                    "slow lateral camera move."
                ),
                "producer": "",
                "expected_submission_time": "",
                "reviewer": "",
                "actual_submission_time": "",
            }
        ],
    }


class DirectorAgentTests(unittest.TestCase):
    def test_parse_storyboard_response_from_json_fence(self):
        result = parse_storyboard_response(
            "分镜如下：\n```json\n"
            + json.dumps(sample_storyboard(), ensure_ascii=False)
            + "\n```"
        )
        self.assertEqual(result.video_title, "第一集·沙暴")
        self.assertEqual(result.shots[0].shot_no, "1")

    def test_reject_invalid_model_response(self):
        with self.assertRaises(DirectorOutputError):
            parse_storyboard_response("我已经看完剧本，但这里不是表格。")

    @patch("app.services.director_agent.llm_chat")
    def test_generation_uses_director_role(self, llm_chat):
        llm_chat.return_value = json.dumps(sample_storyboard(), ensure_ascii=False)
        result = generate_storyboard(
            "陆沉走过被黄沙覆盖的街道。",
            "第一集·沙暴",
            "openai",
        )
        self.assertEqual(result.video_title, "第一集·沙暴")
        self.assertEqual(llm_chat.call_args.kwargs["system_prompt"], DIRECTOR_SYSTEM_PROMPT)
        self.assertIn("被黄沙覆盖", llm_chat.call_args.args[0])

    @patch(
        "app.services.llm_client._resolve",
        return_value={"base_url": "https://llm.example", "api_key": "test-key", "model": "test"},
    )
    @patch("app.services.llm_client.httpx.post")
    def test_chat_sends_director_instructions_as_system_message(
        self, post, _resolve
    ):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        post.return_value = response

        self.assertEqual(
            chat("剧本原文", provider="openai", system_prompt=DIRECTOR_SYSTEM_PROMPT),
            "ok",
        )
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[0], {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT})
        self.assertEqual(messages[1], {"role": "user", "content": "剧本原文"})

    def test_export_matches_storyboard_columns_and_keeps_editable_values(self):
        storyboard = DirectorStoryboardOut.model_validate(sample_storyboard())
        workbook = load_workbook(BytesIO(export_storyboard_xlsx(storyboard)))
        sheet = workbook.active
        self.assertEqual(sheet["B2"].value, "AIGC视频拍摄脚本")
        self.assertEqual(sheet["E4"].value, "第一集·沙暴")
        self.assertEqual(sheet["P6"].value, 1)
        self.assertEqual(sheet["B9"].value, "镜号")
        self.assertEqual(sheet["G9"].value, "画面内容")
        self.assertEqual(sheet["T9"].value, "提示词")
        self.assertEqual(sheet["B10"].value, "1")
        self.assertEqual(sheet["G10"].value, "沙暴掠过废弃街道，陆沉独自走入画面。")
        self.assertEqual(sheet["K10"].value, 6)
        self.assertIn("B2:S2", {str(merged) for merged in sheet.merged_cells.ranges})
        self.assertIn("T10:W10", {str(merged) for merged in sheet.merged_cells.ranges})


if __name__ == "__main__":
    unittest.main()

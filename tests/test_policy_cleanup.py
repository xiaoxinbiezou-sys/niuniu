from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import qa
from pipeline.audio_script import compile_audio_script, validate_audio_script
from pipeline.story_gen import _series_bible_context, build_story_facts


class PolicyCleanupTests(unittest.TestCase):
    def test_story_facts_contain_only_explicit_production_fields(self):
        facts = build_story_facts(
            None, "牛牛不想洗澡", "idea", "family",
            cast={"牛牛": "5岁男孩", "爸爸": "爸爸"},
        )
        self.assertEqual(facts["story_type"], "family")
        self.assertNotIn("policy", facts)

    def test_story_facts_have_no_generated_scale_or_ending_policy(self):
        facts = build_story_facts(
            None, "牛牛变成巨人", "idea", "imagination", cast={"牛牛": "5岁男孩"}
        )
        self.assertNotIn("scale_mode", facts)
        self.assertFalse(facts["required_patterns"])
        self.assertFalse(facts["forbidden_patterns"])

    def test_stale_embedded_audio_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            story = root / "story.md"
            facts = root / "facts.json"
            story.write_text(
                "# 测试\n\n牛牛走进浴室，爸爸已经准备好了水枪。"
                "牛牛抬头问：“真的要洗吗？”爸爸笑着回答：“恐龙也要洗。”",
                encoding="utf-8",
            )
            facts.write_text(json.dumps({
                "story_type": "family",
                "characters": ["牛牛", "爸爸"],
            }, ensure_ascii=False), encoding="utf-8")
            script = compile_audio_script(story, facts)
            script["policy"]["narrator_min_ratio"] = 0.5
            report = validate_audio_script(script)
            self.assertTrue(any("政策已更新" in item for item in report["errors"]))

    def test_explicit_series_bible_is_loaded(self):
        context = _series_bible_context(
            "家庭日常", {"牛牛": "5岁", "添添": "姐姐"},
            "stories/牛牛一家人_儿童故事创作圣经_V1.0.md",
        )
        self.assertIn("二十二、最终目标", context)
        self.assertIn("不强制固定两拍", context)

    def test_legacy_storyboard_pipeline_is_gone(self):
        """旧分镜/重配音流程已整体删除（连可选入口都不再保留）。"""
        with self.assertRaises(ImportError):
            __import__("pipeline.script_gen")
        with self.assertRaises(ImportError):
            __import__("pipeline.make_video")

    @patch("pipeline.qa._qa_key", return_value="")
    def test_image_qa_is_unavailable_not_failed_without_a_key(self, _key):
        """缺 key 是"没法检查"，不是"画面不一致"；以前会被当成失败并白重画三次。"""
        with tempfile.TemporaryDirectory() as temp:
            ref = Path(temp) / "ref.png"
            image = Path(temp) / "image.png"
            ref.write_bytes(b"ref")
            image.write_bytes(b"image")
            verdict = qa.check_image([str(ref)], str(image), {})
            self.assertEqual(verdict["status"], qa.UNAVAILABLE)
            self.assertFalse(verdict["ok"])
            self.assertIn("质检 key", verdict["reason"])


if __name__ == "__main__":
    unittest.main()

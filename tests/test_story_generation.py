from __future__ import annotations

import hashlib
import json
import re
import unittest
from unittest.mock import patch

from pipeline.story_gen import (
    STORY_HARD_MAX,
    STORY_HARD_MIN,
    STORY_TARGET_MAX,
    STORY_TARGET_MIN,
    _parse_story_payload,
    build_story_facts,
    generate_story_package,
    resolve_story_type,
    story_char_range,
)


def _body(chars: int) -> str:
    """造一段指定可见字数的正文（去掉空白后正好 chars 字）。"""
    unit = "牛牛在客厅里玩积木。"
    text = (unit * (chars // len(unit) + 2))[:chars]
    return text


# 故事层以圣经为唯一创作依据，所以每次调用都必须给圣经路径。
# 测试用临时文件，避免依赖 stories/ 下的真实圣经（它以后会改）。
BIBLE_TEXT = "# 测试系列创作圣经\n\n主角是牛牛，5 岁，家庭日常小故事，温暖好笑，不说教。\n"


class StoryGenerationTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.bible = Path(self._tmp.name) / "bible.md"
        self.bible.write_text(BIBLE_TEXT, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_story(self, idea: str = "一个点子", **kwargs):
        """按测试圣经写故事，省得每个用例都传一遍 series_bible。"""
        kwargs.setdefault("story_type", "family")
        return generate_story_package({}, idea, series_bible=str(self.bible), **kwargs)


class DirectStoryGenerationTests(StoryGenerationTestCase):
    def test_story_output_parser_tolerates_format_drift(self):
        """模型不总返回标准 JSON。格式走样不该让一条 3~7 分钟的付费任务作废。"""
        standard = '{"title":"铁罐","text":"牛牛看着铁罐。"}'
        fence = "```"
        cases = [
            (standard, ("铁罐", "牛牛看着铁罐。")),
            (f"{fence}json\n{standard}\n{fence}", ("铁罐", "牛牛看着铁罐。")),
            (f"好的，这是故事：\n{standard}\n希望满意。", ("铁罐", "牛牛看着铁罐。")),
            ("# 铁罐里的种子\n\n牛牛看着铁罐，心里痒痒的。",
             ("铁罐里的种子", "牛牛看着铁罐，心里痒痒的。")),
            # 单行正文没有标题行 —— 整段都必须当正文，不能被当成标题丢掉
            ("牛牛看着铁罐，心里痒痒的。",
             ("未命名故事", "牛牛看着铁罐，心里痒痒的。")),
        ]
        for text, (title, body) in cases:
            with self.subTest(text=text[:20]):
                payload = _parse_story_payload(text)
                self.assertEqual(payload["title"], title)
                self.assertEqual(payload["text"], body)

    def test_empty_model_output_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_story_payload("   \n  ")

    @patch("pipeline.story_gen._generate")
    def test_review_rewrite_retries_instead_of_failing_the_task(self, generate):
        """质检重写撞上模型抽风时要重试——这一稿只差一点，不该整条作废。"""
        body = _body(700)
        generate.side_effect = [
            json.dumps({"title": "十七岁的生日", "text": body}, ensure_ascii=False),
            json.dumps({"pass": False, "issues": ["算不通"]}, ensure_ascii=False),
            "   ",                                            # 重写第 1 次：空输出
            json.dumps({"title": "慢慢长大", "text": body}, ensure_ascii=False),
            json.dumps({"pass": True, "issues": []}, ensure_ascii=False),
        ]
        package = self._write_story("牛牛想每个月都过生日")
        self.assertEqual(package["title"], "慢慢长大")

    @patch("pipeline.story_gen._generate")
    def test_writer_uses_idea_and_bible_and_returns_simple_package(self, generate):
        body = _body(700)
        generate.return_value = json.dumps({"title": "牛牛的魔术", "text": body}, ensure_ascii=False)
        package = self._write_story(
            "牛牛想给家人表演魔术",
            cast={"牛牛": "五岁"}, material_type="idea",
        )
        self.assertEqual(package["title"], "牛牛的魔术")
        self.assertEqual(package["story_pipeline"], "simple")
        self.assertNotIn("story_brief", package)
        self.assertNotIn("story_outline", package)
        self.assertNotIn("segments", package)
        self.assertEqual(package["story_digest"], hashlib.sha256(body.encode()).hexdigest())
        system, user = generate.call_args_list[0].args[1:]
        self.assertIn("牛牛想给家人表演魔术", user)
        # 圣经是【主体】，必须排在最前面，并且真的把内容读进来了
        self.assertIn("系列故事创作圣经", system)
        self.assertIn("主角是牛牛", system)
        self.assertLess(system.index("系列故事创作圣经"),
                        system.index("通用底线"), "圣经要排在通用条款之前")
        self.assertNotIn("StoryBrief", system + user)
        self.assertNotIn("beat", system + user)

    @patch("pipeline.story_gen._generate")
    def test_story_prompt_does_not_impose_audio_form(self, generate):
        """故事层不该被要求写台词引导语/旁白比例——那是脚本层的活。"""
        body = _body(700)
        generate.side_effect = [json.dumps({"title": "标题", "text": body}, ensure_ascii=False),
                                json.dumps({"pass": True, "issues": []}, ensure_ascii=False)]
        self._write_story()
        system = generate.call_args_list[0].args[1]
        self.assertNotIn("每句台词前必须", system)
        self.assertNotIn("旁白占", system)
        self.assertIn("不需要考虑配音", system)

    @patch("pipeline.story_gen._generate")
    def test_missing_bible_is_an_error_not_a_silent_downgrade(self, generate):
        """读不到圣经必须报错：故事层以圣经为唯一创作依据，不能悄悄裸写。"""
        with self.assertRaises(ValueError) as ctx:
            generate_story_package({}, "一个点子", story_type="family",
                                   series_bible="stories/不存在的圣经.md")
        self.assertIn("圣经", str(ctx.exception))
        generate.assert_not_called()

    def test_char_range_follows_the_bible(self):
        """字数由圣经规定：点子目标 600~800，硬范围 500~900。"""
        self.assertEqual(story_char_range("idea"), (600, 800))
        self.assertEqual(story_char_range("complete"), (500, 900))
        self.assertEqual((STORY_TARGET_MIN, STORY_TARGET_MAX), (600, 800))
        self.assertEqual((STORY_HARD_MIN, STORY_HARD_MAX), (500, 900))

    @patch("pipeline.story_gen._generate")
    def test_text_inside_target_range_is_accepted_without_rewrite(self, generate):
        """落在圣经目标区间内的稿子直接通过，不为字数重写。

        调用次数 = 1 次写故事 + 1 次轻量自检。
        """
        payloads = [json.dumps({"title": "标题", "text": _body(650)}, ensure_ascii=False),
                    json.dumps({"pass": True, "issues": []}, ensure_ascii=False)]
        generate.side_effect = payloads
        package = self._write_story()
        self.assertEqual(len(re.sub(r"\s+", "", package["text"])), 650)
        self.assertEqual(generate.call_count, 2)
        self.assertNotIn("上一版存在以下问题", generate.call_args_list[1].args[2])

    @patch("pipeline.story_gen._generate")
    def test_over_length_triggers_rewrite_then_accepts(self, generate):
        generate.side_effect = [
            json.dumps({"title": "标题", "text": _body(1000)}, ensure_ascii=False),
            json.dumps({"title": "标题", "text": _body(700)}, ensure_ascii=False),
            json.dumps({"pass": True, "issues": []}, ensure_ascii=False),
        ]
        package = self._write_story()
        self.assertEqual(len(re.sub(r"\s+", "", package["text"])), 700)
        self.assertEqual(generate.call_count, 3)

    @patch("pipeline.story_gen._generate")
    def test_too_short_story_is_rejected(self, generate):
        generate.return_value = json.dumps({"title": "标题", "text": _body(STORY_HARD_MIN - 1)},
                                           ensure_ascii=False)
        with self.assertRaises(ValueError):
            self._write_story()

    @patch("pipeline.story_gen._generate")
    def test_story_keeps_retrying_while_length_is_off(self, generate):
        """一直不达标时按次数上限重试，最后失败（不会无限循环）。"""
        generate.return_value = json.dumps({"title": "标题", "text": _body(1000)},
                                           ensure_ascii=False)
        with self.assertRaises(ValueError):
            self._write_story()
        self.assertLessEqual(generate.call_count, 6)

    @patch("pipeline.story_gen._generate")
    def test_writer_rejects_empty_text(self, generate):
        generate.return_value = json.dumps({"title": "标题", "text": ""})
        with self.assertRaises(ValueError):
            self._write_story()

    @patch("pipeline.story_gen._generate")
    def test_light_review_rewrites_once_when_common_sense_issue_found(self, generate):
        body = _body(700)
        generate.side_effect = [
            json.dumps({"title": "十七岁的生日", "text": body}, ensure_ascii=False),
            json.dumps({"pass": False, "issues": ["没有说明五岁加十二岁为何是十七岁"]},
                       ensure_ascii=False),
            json.dumps({"title": "慢慢长大", "text": body}, ensure_ascii=False),
            json.dumps({"pass": True, "issues": []}, ensure_ascii=False),
        ]
        package = self._write_story("牛牛想每个月都过生日")
        self.assertEqual(package["title"], "慢慢长大")
        self.assertEqual(generate.call_count, 4)
        self.assertIn("没有说明五岁加十二岁为何是十七岁", generate.call_args_list[2].args[2])

    @patch("pipeline.story_gen._generate")
    def test_review_failure_does_not_block_story(self, generate):
        body = _body(700)
        generate.side_effect = [
            json.dumps({"title": "吃蛋糕", "text": body}, ensure_ascii=False),
            RuntimeError("review unavailable"),
        ]
        package = self._write_story("牛牛想吃蛋糕")
        self.assertEqual(package["text"], body)
        self.assertEqual(generate.call_count, 2)

    def test_story_type_alias_and_facts_are_generic(self):
        self.assertEqual(resolve_story_type("fantasy", "任意点子"), "imagination")
        facts = build_story_facts(None, "牛牛想飞", "idea", "imagination", {"牛牛": "五岁"})
        self.assertEqual(facts["required_patterns"], [])
        self.assertNotIn("scale_mode", facts)


if __name__ == "__main__":
    unittest.main()

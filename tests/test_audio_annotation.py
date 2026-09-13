"""脚本改编层测试：AI 逐条判断"每个片段谁念"，程序按片段装配并校验。

职责边界（这一层存在的理由）：
  - 故事层只负责好看，不承担任何音频形式要求；
  - 本层负责改编：断句、判断说话人、决定对白还是旁白；
  - 程序只做两件机械的事：把片段切成可引用的编号、按编号取原文装配。

**为什么模型只做"逐条判断题"**：实测让它把编号"分组成段落"时，它会写出互相重叠、
覆盖不全的分组（旁白段横跨全部片段），怎么加校验都拼不出脚本。改成逐条选择题后
任务变简单，而结构由程序保证——正文按片段区间取出，**重复或漏字不可能发生**。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from pipeline import audio_annotator, audio_script as A

FACTS = {"story_type": "family",
         "characters": [{"name": n} for n in ("牛牛", "爸爸", "妈妈", "添添")]}

BODY = (
    "晚上，厨房里发现一只大虫子。爸爸拍拍胸脯说：“我什么都不怕！”"
    "牛牛跟在爸爸后面。“虫子在哪儿？”爸爸问。"
)


def _blocks(body: str = BODY):
    return A.split_sentences(body)


def _compile(decisions: dict[int, dict], body: str = BODY) -> dict:
    return A._compile_from_segments("t", body, FACTS, "", "", decisions, {},
                                    indexed=_blocks(body))


class SplitterTests(unittest.TestCase):
    def test_blocks_rebuild_the_story_exactly(self):
        """片段只是"可引用的编号"，拼起来必须逐字等于母稿。"""
        for name, body in {
            "前置引导": '牛牛说：“我去。”妈妈问：“去哪？”',
            "后置引导": '牛牛笑了一下。“我去。”牛牛说。',
            "单字台词": '牛牛磕鸡蛋。“啪！”蛋壳掉进锅里。',
            "多段落": '第一段。\n\n第二段。“台词。”\n\n第三段。',
        }.items():
            with self.subTest(name):
                blocks = A.split_sentences(body)
                self.assertEqual("".join(b[2] for b in blocks), body)
                self.assertTrue(all(b[2].strip() for b in blocks))

    def test_quotes_become_their_own_blocks(self):
        blocks = [b[2] for b in _blocks()]
        self.assertIn("“我什么都不怕！”", blocks)
        self.assertIn("“虫子在哪儿？”", blocks)


class PlanTests(unittest.TestCase):
    def _payload(self, items):
        return json.dumps({"items": items}, ensure_ascii=False)

    def test_plan_reads_per_block_decisions(self):
        payload = self._payload([
            {"index": 0, "type": "narrator"},
            {"index": 1, "type": "narrator"},
            {"index": 2, "type": "dialogue", "speaker": "爸爸", "emotion": "brave"},
            {"index": 3, "type": "narrator"},
            {"index": 4, "type": "dialogue", "speaker": "爸爸", "emotion": "scared"},
            {"index": 5, "type": "narrator"},
        ])
        with patch("pipeline.audio_annotator.chat_text", return_value=payload):
            plan, error = audio_annotator.plan_script({}, ["a", "b", "c", "d", "e", "f"],
                                                      ["爸爸"])
        self.assertEqual(error, "")
        self.assertEqual(plan[2], {"type": "dialogue", "speaker": "爸爸",
                                   "emotion": "brave"})
        self.assertEqual(plan[0], {"type": "narrator"})

    def test_unsupported_emotion_is_dropped_not_passed_to_tts(self):
        """模型自己造的情绪不能透传给配音引擎，否则那句话会退化成平淡朗读。"""
        payload = self._payload([
            {"index": 0, "type": "dialogue", "speaker": "爸爸", "emotion": "暴跳如雷"},
        ])
        with patch("pipeline.audio_annotator.chat_text", return_value=payload):
            plan, _ = audio_annotator.plan_script({}, ["a"], ["爸爸"])
        self.assertEqual(plan[0], {"type": "dialogue", "speaker": "爸爸"})
        self.assertNotIn("emotion", plan[0])

    def test_speaker_outside_the_cast_is_ignored_not_trusted(self):
        payload = self._payload([{"index": 0, "type": "dialogue", "speaker": "隔壁老王"}])
        with patch("pipeline.audio_annotator.chat_text", return_value=payload):
            plan, error = audio_annotator.plan_script({}, ["a"], ["爸爸"])
        # 名字不在人物表里 → 这一条不采纳（当成旁白），而不是把编造的角色塞进脚本
        self.assertEqual(plan.get(0), None)
        self.assertIn("没有可用", error)

    def test_too_few_decisions_is_rejected(self):
        payload = self._payload([{"index": 0, "type": "narrator"}])
        with patch("pipeline.audio_annotator.chat_text", return_value=payload):
            plan, error = audio_annotator.plan_script({}, ["a", "b", "c", "d"], ["爸爸"])
        self.assertEqual(plan, {})
        self.assertIn("覆盖太少", error)

    def test_service_failure_does_not_raise(self):
        with patch("pipeline.audio_annotator.chat_text", side_effect=RuntimeError("网关抽风")):
            plan, error = audio_annotator.plan_script({}, ["a"], ["爸爸"])
        self.assertEqual(plan, {})
        self.assertIn("网关抽风", error)


class AssembleTests(unittest.TestCase):
    def test_unannotated_blocks_become_narration(self):
        script = _compile({})
        self.assertEqual(len(script["segments"]), 1)
        self.assertEqual(script["segments"][0]["type"], "narrator")

    def test_consecutive_narration_blocks_merge_into_one_segment(self):
        script = _compile({})
        self.assertEqual(len(script["segments"]), 1)
        # 引号只用于标记台词，不参与朗读，所以旁白段里不留引号
        self.assertEqual(script["segments"][0]["text"], A._strip_quotes(BODY))

    def test_dialogue_block_drops_its_quotes_and_keeps_the_speaker(self):
        blocks = _blocks()
        index = next(i for i, b in enumerate(blocks) if b[2] == "“我什么都不怕！”")
        script = _compile({index: {"type": "dialogue", "speaker": "爸爸"}})
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertEqual(len(dialogue), 1)
        self.assertEqual(dialogue[0]["text"], "我什么都不怕！")
        self.assertEqual(dialogue[0]["speaker"], "爸爸")

    def test_emotion_from_the_plan_reaches_the_segment(self):
        """台词情绪由改编层（AI）给出——它读得到全文上下文。

        这条是防回归的：曾经装配时给情绪推断函数传了空上下文，
        53 句台词全部变成 neutral，配音听起来毫无起伏。
        """
        blocks = _blocks()
        index = next(i for i, b in enumerate(blocks) if b[2] == "“我什么都不怕！”")
        script = _compile({index: {"type": "dialogue", "speaker": "爸爸",
                                   "emotion": "brave"}})
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertEqual(dialogue[0]["emotion"], "brave")

    def test_emotion_falls_back_to_context_when_the_plan_omits_it(self):
        """改编层没给情绪时，退回到"用前后旁白推断"，而不是一律 neutral。"""
        body = "牛牛吓得差点从板凳上掉下来。牛牛喊：“它还会飞！”"
        blocks = A.split_sentences(body)
        index = next(i for i, b in enumerate(blocks) if b[2].startswith("“"))
        script = _compile({index: {"type": "dialogue", "speaker": "牛牛"}}, body)
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertNotEqual(dialogue[0]["emotion"], "neutral",
                            "有'吓得'这种上下文时不该判成平淡")

    def test_source_ranges_never_overlap(self):
        """相邻段落首尾相接是正常的（起点=上一段终点），但不许重叠。"""
        blocks = _blocks()
        decisions = {i: {"type": "dialogue", "speaker": "爸爸"}
                     for i, b in enumerate(blocks) if b[2].startswith("“")}
        script = _compile(decisions)
        prev_end = 0
        for seg in script["segments"]:
            start = seg["source"]["start"]
            end = seg["source"]["end"]
            self.assertGreaterEqual(start, prev_end, f"区间重叠：{seg['text'][:16]!r}")
            self.assertLess(start, end, "区间为空")
            prev_end = end

    def test_assembled_script_covers_the_story_and_passes_validation(self):
        blocks = _blocks()
        decisions = {i: {"type": "dialogue", "speaker": "爸爸"}
                     for i, b in enumerate(blocks) if b[2].startswith("“")}
        script = _compile(decisions)
        report = A.validate_audio_script(script, story_body=BODY, facts=FACTS)
        self.assertTrue(report["ok"], report["errors"])
        self.assertEqual("".join(s["text"] for s in script["segments"]),
                         A._strip_quotes(BODY))

    def test_turning_a_line_into_narration_lifts_the_ratio(self):
        blocks = _blocks()
        quote_index = next(i for i, b in enumerate(blocks) if b[2] == "“我什么都不怕！”")
        with_line = _compile({quote_index: {"type": "dialogue", "speaker": "爸爸"}})
        without = _compile({})
        r1 = A.validate_audio_script(with_line, story_body=BODY, facts=FACTS)["metrics"]
        r2 = A.validate_audio_script(without, story_body=BODY, facts=FACTS)["metrics"]
        self.assertGreater(r2["narrator_ratio"], r1["narrator_ratio"])

    def test_short_interjection_between_narration_is_not_a_separate_voice(self):
        """'爸爸“哇”地跳上沙发' 里的"哇"只有一声，不该单独占一段配音。

        否则会切成 '爸爸' / '“哇”' / '地跳上沙发，大叫：' 三段：
        为一声"哇"单独合成一次音频，听感也碎。夹在旁白中间的极短语气词要并回旁白。
        """
        body = "小甲虫忽然张开翅膀，爸爸“哇”地跳上沙发，大叫：“快把它弄走！”"
        blocks = A.split_sentences(body)
        decisions = {}
        for i, b in enumerate(blocks):
            if b[2] == "“哇”":
                decisions[i] = {"type": "dialogue", "speaker": "爸爸"}
            elif b[2].startswith("“"):
                decisions[i] = {"type": "dialogue", "speaker": "爸爸"}
        script = _compile(decisions, body)
        texts = [s["text"] for s in script["segments"] if s["type"] == "narrator"]
        self.assertTrue(any("爸爸哇地跳上沙发" in t for t in texts),
                        f"语气词没有并入旁白：{texts}")
        self.assertNotIn("哇", [s["text"] for s in script["segments"]
                                if s["type"] == "dialogue"])


class FallbackTests(unittest.TestCase):
    def test_adaptation_failure_falls_back_to_deterministic_compile(self):
        """改编失败不能让流程中断：必须回退到规则编译，并记下原因。"""
        with patch("pipeline.audio_annotator.chat_text", side_effect=RuntimeError("网关抽风")):
            script = A.compile_with_annotation(
                "t", BODY, FACTS, "", "", llm_cfg={"provider": "x"})
        self.assertTrue(script["segments"])
        self.assertIn("网关抽风", script["adaptation"]["error"])

    def test_rules_only_compile_still_works(self):
        script = A.compile_with_annotation("t", BODY, FACTS, "", "", llm_cfg=None)
        self.assertTrue(script["segments"])
        self.assertNotIn("adaptation", script)


if __name__ == "__main__":
    unittest.main()

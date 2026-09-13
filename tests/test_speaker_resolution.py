from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.audio_script import (
    _guide_segment,
    compile_audio_script,
    validate_audio_script,
)

ALIASES = {"牛牛": "牛牛", "小蓝鱼": "小蓝鱼", "妈妈": "妈妈", "爸爸": "爸爸"}


class SpeakerResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facts_path = self.root / "facts.json"
        self.facts = {
            "story_type": "family",
            "characters": [{"name": n} for n in ("牛牛", "添添", "爸爸", "妈妈")],
        }
        self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _compile(self, body: str) -> dict:
        story = self.root / "story.md"
        story.write_text(f"# 测试\n\n{body}\n", encoding="utf-8")
        return compile_audio_script(story, self.facts_path)

    def _dialogues(self, body: str):
        return [s for s in self._compile(body)["segments"] if s["type"] == "dialogue"]

    def test_speech_verb_with_object_before_colon(self):
        # "添添赶紧问爸爸：" — "爸爸" is the listener, not the speaker.
        dialogues = self._dialogues("添添赶紧问爸爸：“爸爸，苗苗吃什么呢？”爸爸笑了。")
        self.assertEqual(dialogues[0]["speaker"], "添添")

    def test_danda_verb_now_recognised(self):
        # "念叨"/"嘟囔" were missing from the verb list, so the guide looked verbless.
        dialogues = self._dialogues("牛牛围着铁罐转了八圈，嘴里念叨：“你是不是睡过头啦？”妈妈笑了。")
        self.assertEqual(dialogues[0]["speaker"], "牛牛")

    def test_verb_object_still_not_mistaken_for_the_listener(self):
        dialogues = self._dialogues("妈妈对牛牛说：“快去洗手。”牛牛点头。")
        self.assertEqual(dialogues[0]["speaker"], "妈妈")

    def test_guide_is_stitched_across_a_split_sentence(self):
        # The narration cap can cut one sentence into two narrator segments, which used to
        # hand the resolver only a mid-sentence fragment. Long narration forces that split.
        filler = "牛牛在河边玩，忽然看见一条小蓝鱼被水草缠住了，尾巴直扑腾。" * 2
        body = (f"{filler}牛牛赶紧伸手，轻轻解开缠住的水草，"
                f"小蓝鱼地游出来，说：“谢谢你！我带你飞吧！”")
        segments = self._compile(body)["segments"]
        index = next(i for i, s in enumerate(segments) if s["type"] == "dialogue")
        guide, parts = _guide_segment(segments, index)
        self.assertGreaterEqual(parts, 2, "the split sentence was not stitched back")
        self.assertIn("小蓝鱼", guide)
        self.assertTrue(guide.endswith("说："))

    def test_name_split_from_its_verb_is_not_guessed(self):
        # "……水草，小蓝鱼" / "地游出来，吐了一串泡泡：" has no speech verb at all. Nothing
        # in the母稿 says who speaks, so the resolver must return no speaker rather than
        # picking the nearest name.
        body = ("牛牛赶紧伸手，轻轻解开缠住的水草，小蓝鱼地游出来，"
                "吐了一串亮晶晶的泡泡：“谢谢你！我带你飞吧！”")
        dialogues = self._dialogues(body)
        self.assertEqual(dialogues[0]["speaker"], "")

    def test_action_clause_with_a_speech_verb_still_attributes(self):
        # "牛牛急得围着铁罐转了八圈，嘴里念叨：" — the comma leads straight into the speech
        # verb, so the母稿 is naming 牛牛 as the speaker and the line is attributed.
        body = ("到了第七天，牛牛急得围着铁罐转了八圈，嘴里念叨："
                "“小种子，你是不是睡过头啦？”牛牛又等了一天。")
        self.assertEqual(self._dialogues(body)[0]["speaker"], "牛牛")

    def test_clause_without_a_speech_verb_is_not_attributed(self):
        # "……牛牛赶紧伸手，……小蓝鱼地游出来，吐了一串泡泡：" has no speech verb anywhere,
        # so nothing in the母稿 says who speaks and the resolver must give up.
        body = ("牛牛赶紧伸手，轻轻解开缠住的水草，小蓝鱼地游出来，"
                "吐了一串亮晶晶的泡泡：“谢谢你！我带你飞吧！”")
        self.assertEqual(self._dialogues(body)[0]["speaker"], "")
        report = validate_audio_script(self._compile(body))
        self.assertTrue(any("speaker 无法确定" in e for e in report["errors"]), report["errors"])

    def test_character_missing_from_facts_is_reported_by_name(self):
        body = "萤火虫飞来了，围着小狐狸转圈圈。萤火虫说：“别难过，我们帮你照亮回家的路。”小狐狸笑了。"
        report = validate_audio_script(self._compile(body))
        self.assertFalse(report["ok"])
        self.assertTrue(any("萤火虫" in e for e in report["errors"]),
                        f"error should name the missing character: {report['errors']}")

    def test_adding_the_character_to_facts_resolves_it(self):
        body = "萤火虫飞来了，围着小狐狸转圈圈。萤火虫说：“别难过，我们帮你照亮回家的路。”小狐狸笑了。"
        story = self.root / "story.md"
        story.write_text(f"# 测试\n\n{body}\n", encoding="utf-8")
        facts = dict(self.facts)
        facts["characters"] = list(self.facts["characters"]) + [{"name": "萤火虫"}]
        facts_path = self.root / "facts2.json"
        facts_path.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
        script = compile_audio_script(story, facts_path)
        dialogue = next(s for s in script["segments"] if s["type"] == "dialogue")
        self.assertEqual(dialogue["speaker"], "萤火虫")

    def test_pronoun_line_without_a_previous_speaker_is_rejected(self):
        # "他说：" cannot be attributed when nothing was said before; failing closed beats
        # handing the line to the wrong voice.
        report = validate_audio_script(self._compile("爸爸笑了。他说：“我们回家吧。”牛牛点头。"))
        self.assertFalse(report["ok"])
        self.assertTrue(any("speaker 无法确定" in e for e in report["errors"]), report["errors"])

    def test_pronoun_line_after_a_known_speaker(self):
        dialogues = self._dialogues("爸爸笑了。爸爸说：“走吧。”他说：“好。”牛牛点头。")
        self.assertGreaterEqual(len(dialogues), 1)
        self.assertEqual(dialogues[0]["speaker"], "爸爸")

    def test_consecutive_quotes_report_a_missing_guide(self):
        report = validate_audio_script(self._compile("牛牛说：“我来了。”“等等我。”"))
        # 第二句前面没有旁白引导，说话人定不出来（引导语缺失本身只作提示，
        # 但说话人空缺是硬错误——配音不知道该用谁的声音）。
        self.assertFalse(report["ok"])
        self.assertTrue(any("无法确定" in e for e in report["errors"]), report["errors"])

    def test_postposition_guide_is_accepted(self):
        """'「…」爸爸问。' 是规范中文写法，模型也常这么写，必须能认出说话人。"""
        body = ('晚上，厨房里发现一只大虫子。爸爸拍拍胸脯说：“我什么都不怕！”'
                '牛牛跟在爸爸后面。“虫子在哪儿？”爸爸问。'
                '忽然虫子飞过来。“哇——”爸爸大叫一声，跳上了椅子。'
                '“我这是在保护牛牛！”爸爸站在椅子上，嘴硬地说。'
                '牛牛小声说：“爸爸，你刚才抖得好厉害。”')
        report = validate_audio_script(self._compile(body))
        self.assertTrue(report["ok"], report["errors"])
        # 这三句的引导语里没有神态词（问/大叫/嘴硬地说），情绪保持 neutral 是预期的
        self.assertEqual(report["metrics"]["neutral_dialogue_segments"], 3)

    def test_next_line_guide_is_not_borrowed_by_the_previous_line(self):
        """引号后面跟的是"下一句台词的引导语"时，不能被当成这一句的后置说明。"""
        body = ('牛牛瞪大眼睛，然后咯咯咯地笑起来：“原来我一直在给泥土浇水呀！”'
                '爸爸挠挠头，不好意思地笑了：“哎呀，爸爸忘记放种子啦！”')
        report = validate_audio_script(self._compile(body))
        self.assertFalse(any("母稿语法指向" in e for e in report["errors"]), report["errors"])

    def test_long_guide_keeps_its_subject(self):
        """引导语超过 80 字时，句首主语不能被截断丢掉。"""
        body = ('爸爸走过来，打开铁罐一看，愣住了——里面只有泥土，别的什么都没有！'
                '爸爸挠挠头，不好意思地笑了：“哎呀，爸爸忘记放种子啦！”')
        dialogues = self._dialogues(body)
        self.assertEqual(dialogues[0]["speaker"], "爸爸")

    def test_compiler_and_validator_agree_on_every_line(self):
        body = (
            "牛牛钻进妈妈怀里，认真地说：“我还是先好好当小孩吧。”妈妈听完笑了。"
            "添添赶紧问爸爸：“爸爸，苗苗吃什么呢？”爸爸挠挠头，不好意思地笑了："
            "“爸爸忘记啦！”牛牛拍着手喊：“发芽啦！”"
        )
        script = self._compile(body)
        report = validate_audio_script(script, story_body=body, facts=self.facts)
        self.assertFalse(any("母稿语法指向" in e for e in report["errors"]), report["errors"])


if __name__ == "__main__":
    unittest.main()

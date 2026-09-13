from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pipeline.audio_script import (
    approval_digest,
    approve_script,
    compile_audio_script,
    validate_fact_text,
    validate_audio_script,
)


class AudioScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.facts_path = self.root / "facts.json"
        self.facts = {
            "story_type": "family",
            "characters": [
                {"name": "牛牛", "aliases": []},
                {"name": "妈妈", "aliases": []},
                {"name": "爸爸", "aliases": []},
                {"name": "梦梦", "aliases": ["小人"]},
            ],
            "forbidden_patterns": [
                {"pattern": "梦梦小得像黑芝麻", "message": "梦梦尺寸错误"}
            ],
        }
        self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _story(self, body: str) -> Path:
        path = self.root / "story.md"
        path.write_text(f"# 测试故事\n\n{body}\n", encoding="utf-8")
        return path

    def test_prose_is_narration_and_quotes_are_dialogue(self):
        story = self._story(
            "牛牛在客厅玩了很久，窗外慢慢暗下来。"
            "牛牛放下积木，认真地说：“我来收拾玩具。”"
            "妈妈听见以后笑了，客厅很快又变得整整齐齐。"
        )
        script = compile_audio_script(story, self.facts_path)
        report = validate_audio_script(script)
        self.assertTrue(report["ok"], report["errors"])
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertEqual([s["speaker"] for s in dialogue], ["牛牛"])
        self.assertTrue(all(s["speaker"] == "旁白" for s in script["segments"] if s["type"] == "narrator"))

    def test_clause_subject_wins_over_object_name(self):
        story = self._story(
            "天亮以后，牛牛终于从梦里醒来。"
            "牛牛钻进妈妈怀里，认真地说：“我还是先好好当小孩吧。”"
            "妈妈听完笑了，牵着他去吃早饭。"
        )
        script = compile_audio_script(story, self.facts_path)
        dialogue = next(s for s in script["segments"] if s["type"] == "dialogue")
        self.assertEqual(dialogue["speaker"], "牛牛")
        self.assertTrue(validate_audio_script(script)["ok"])

    def test_natural_speech_verbs_resolve_character(self):
        story = self._story(
            "牛牛围着铁罐转了八圈，嘴里念叨：“小种子，你是不是睡过头啦？”"
            "爸爸挠挠头，不好意思地笑了：“爸爸忘记放种子啦！”"
            "牛牛咯咯咯地笑起来：“原来我一直在给泥土浇水呀！”"
        )
        script = compile_audio_script(story, self.facts_path)
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertEqual([s["speaker"] for s in dialogue], ["牛牛", "爸爸", "牛牛"])

    def test_long_dialogue_is_warning_not_blocking_error(self):
        story = self._story(
            "早晨的阳光照进客厅，牛牛坐在窗边等爸爸。窗台上摆着几盆绿油油的植物，空气里有淡淡的泥土香。爸爸拿来一个小铁罐，认真地说：“这里面有草莓种子，你好好照顾它，就能长出红红的草莓！”牛牛抱住铁罐，开心地跑到窗边。"
        )
        script = compile_audio_script(story, self.facts_path)
        report = validate_audio_script(script)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(any("台词较长" in warning for warning in report["warnings"]))

    def test_source_text_and_speaker_drift_are_blocked(self):
        story = self._story(
            "牛牛走到窗边，看见雨已经停了。"
            "牛牛高兴地喊：“我们可以出去玩啦！”"
            "妈妈拿来外套，陪他一起走到门口。"
        )
        script = compile_audio_script(story, self.facts_path)
        dialogue = next(s for s in script["segments"] if s["type"] == "dialogue")
        dialogue["text"] = "妈妈替他回答了。"
        report = validate_audio_script(script)
        self.assertFalse(report["ok"])
        self.assertTrue(any("母稿来源不一致" in e for e in report["errors"]))

    def test_unknown_speaker_is_blocked(self):
        """说话人由 AI 标注决定，但必须是人物表里的角色——编造的角色要拦下来。"""
        story = self._story(
            "牛牛走到窗边，看见雨已经停了。"
            "牛牛高兴地喊：“我们可以出去玩啦！”"
            "妈妈拿来外套，陪他一起走到门口。"
        )
        script = compile_audio_script(story, self.facts_path)
        dialogue = next(s for s in script["segments"] if s["type"] == "dialogue")
        dialogue["speaker"] = "隔壁老王"
        report = validate_audio_script(script)
        self.assertFalse(report["ok"])
        self.assertTrue(any("隔壁老王" in e and "不在人物事实表中" in e
                            for e in report["errors"]), report["errors"])

    def test_forbidden_world_fact_blocks_script(self):
        story = self._story(
            "牛牛低头一看，梦梦小得像黑芝麻。"
            "梦梦仰着头喊：“你真高呀！”"
            "牛牛蹲下来，认真听她说话。"
        )
        script = compile_audio_script(story, self.facts_path)
        report = validate_audio_script(script)
        self.assertFalse(report["ok"])
        self.assertTrue(any("梦梦尺寸错误" in e for e in report["errors"]))

    @unittest.skip("Story-specific repetition rule was removed")
    def test_repeated_action_cannot_enter_audio(self):
        story = self._story(
            "牛牛看看澡盆，又看看水枪，挺起小胸脯。"
            "牛牛挺起小胸脯喊：“恐龙宝宝可以洗！”"
            "妈妈听完笑了。"
        )
        script = compile_audio_script(story, self.facts_path)
        report = validate_audio_script(script)
        self.assertFalse(report["ok"])
        self.assertTrue(any("重复描述动作" in error for error in report["errors"]))

    def test_plain_story_fact_validation_is_reusable_before_compilation(self):
        issues = validate_fact_text("梦梦站在手心里，像一只小跳蚤。", {
            "forbidden_patterns": [
                {"pattern": "梦梦.{0,20}像(?:一只)?小跳蚤", "message": "禁止新增尺寸参照"}
            ]
        })
        self.assertEqual(issues, ["事实冲突：禁止新增尺寸参照"])

    def test_approval_digest_blocks_post_approval_edit(self):
        story = self._story(
            "牛牛在院子里跑了几圈，终于学会了跳绳。"
            "牛牛举起跳绳喊：“妈妈，我学会啦！”"
            "妈妈走过来抱住他，两个人都笑了。"
        )
        script = approve_script(compile_audio_script(story, self.facts_path))
        self.assertEqual(script["approval"]["digest"], approval_digest(script))
        self.assertTrue(validate_audio_script(script, require_approved=True)["ok"])
        script["segments"][0]["emotion"] = "excited"
        report = validate_audio_script(script, require_approved=True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("批准后脚本发生变化" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()

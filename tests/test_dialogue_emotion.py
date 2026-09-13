from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from pipeline import tts
from pipeline.audio_script import (
    DEFAULT_DIALOGUE_EMOTION,
    ENGINE_EMOTIONS,
    approve_script,
    compile_audio_script,
    guess_dialogue_emotion,
    to_storyboard,
    validate_audio_script,
)

# Every quoted line in this story is introduced by narration that already states how
# it is delivered, so the compiler must not fall back to a flat "neutral" reading.
STORY = (
    "牛牛今年五岁。一天早上，爸爸送给他一个小铁罐，神秘地说：“这里面有草莓种子，"
    "你好好照顾它，就能长出红红的草莓！”"
    "牛牛高兴极了。他把铁罐放在窗台上，每天早上一睁眼，第一件事就是给铁罐浇水。"
    "一天过去了，没有发芽。三天过去了，还是没有发芽。到了第七天，牛牛急得围着铁罐转了八圈，"
    "嘴里念叨：“小种子，小种子，你是不是睡过头啦？”"
    "爸爸走过来，打开铁罐一看，愣住了——里面只有泥土，别的什么都没有！"
    "爸爸挠挠头，不好意思地笑了：“哎呀，爸爸忘记把种子放进去啦！”"
    "牛牛瞪大眼睛，然后咯咯咯地笑起来：“原来我这一个星期，一直在给泥土浇水呀！”"
    "父子俩笑成一团。第二天，爸爸真的买来了草莓种子。牛牛亲手把种子埋进土里，轻轻浇上水。"
    "这一次，他等呀等，过了几天，土里真的钻出了两瓣嫩嫩的小绿芽。"
    "牛牛拍着手喊：“发芽啦！发芽啦！这次的种子睡醒啦！”"
)
FACTS = {
    "story_type": "family",
    "characters": [{"name": "牛牛"}, {"name": "爸爸"}, {"name": "妈妈"}, {"name": "添添"}],
}


class DialogueEmotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.story = root / "story.md"
        self.facts = root / "facts.json"
        self.story.write_text(f"# 草莓种子\n\n{STORY}\n", encoding="utf-8")
        self.facts.write_text(json.dumps(FACTS, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _script(self) -> dict:
        return compile_audio_script(self.story, self.facts)

    def test_reported_story_gets_an_emotion_on_every_line(self):
        script = self._script()
        dialogue = [s for s in script["segments"] if s["type"] == "dialogue"]
        self.assertEqual(len(dialogue), 5)
        self.assertEqual(
            [s["emotion"] for s in dialogue],
            ["mystery", "anxious", "awkward", "laugh", "excited"],
        )
        self.assertNotIn(DEFAULT_DIALOGUE_EMOTION, [s["emotion"] for s in dialogue])

    def test_every_emotion_is_renderable_by_the_engines(self):
        script = self._script()
        for segment in script["segments"]:
            self.assertIn(segment["emotion"], ENGINE_EMOTIONS)
        for segment in script["segments"]:
            if segment["type"] != "dialogue":
                continue
            self.assertIn(segment["emotion"], tts._DOUBAO_EMO_INSTRUCT)

    def test_emotion_reaches_the_doubao_request_body(self):
        storyboard = to_storyboard(approve_script(self._script()))
        scene = next(sc for sc in storyboard["scenes"] if sc["emotion"] == "awkward"
                     and sc["speaker"] == "爸爸")
        vc = {"engine": "doubao", "voice_type": "zh_male_wennuanahu_uranus_bigtts",
              "x_api_key": "test-key", "resource_id": "seed-tts-2.0"}
        captured: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"data":"QUJD"}'  # one base64 chunk is enough

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with patch.object(urllib.request, "urlopen", fake_urlopen), \
                patch("pipeline.tts._mp3_to_wav"), \
                patch("pipeline.tts._trim_tail_silence"):
            out = Path(self.temp.name)
            try:
                # _synth_doubao is the layer that turns scene["emotion"] into the
                # context_texts voice instruction the Doubao 2.0 API consumes.
                tts._synth_doubao([scene], vc, out)
            except Exception:
                pass  # the mocked transport is enough; we only inspect the request
        self.assertIn("body", captured, "no request was issued")
        additions = json.loads(captured["body"]["req_params"]["additions"])
        instruction = " ".join(additions.get("context_texts") or [])
        self.assertIn("不好意思", instruction)
        self.assertIn("尴尬", instruction)

    def test_mystery_and_awkward_are_distinct_from_surprise(self):
        self.assertEqual(guess_dialogue_emotion("爸爸神秘地说："), "mystery")
        self.assertEqual(guess_dialogue_emotion("爸爸挠挠头，不好意思地笑了："), "awkward")
        self.assertEqual(guess_dialogue_emotion("牛牛吃惊地瞪大眼睛："), "surprise")
        self.assertEqual(guess_dialogue_emotion("牛牛急得围着铁罐转了八圈，嘴里念叨："), "anxious")

    def test_guide_emotion_is_not_dragged_in_from_an_earlier_beat(self):
        # "愣住" belongs to the previous beat; the clause holding the speech verb wins.
        self.assertEqual(
            guess_dialogue_emotion("一愣！爸爸挠挠头，不好意思地笑了："), "awkward")

    def test_cue_is_found_even_when_the_guide_opens_with_it(self):
        # The emotion sits in the first clause and four action clauses follow it, so a
        # "last matching clause" scan would have to reach back past all of them.
        guide = ("牛牛高兴极了。他把铁罐放在窗台上。一天过去了。三天过去了。"
                 "到了第七天，他围着铁罐转了八圈，嘴里念叨：")
        self.assertEqual(guess_dialogue_emotion(guide, "种子呀种子"), "excited")

    def test_quote_wording_is_only_used_when_the_guide_is_emotionless(self):
        self.assertEqual(guess_dialogue_emotion("妈妈问：", "你吃了吗？"), "question")
        self.assertEqual(guess_dialogue_emotion("牛牛说：", "今天天气不错。"),
                         DEFAULT_DIALOGUE_EMOTION)

    def test_plain_neutral_line_is_reported_as_a_warning(self):
        path = Path(self.temp.name) / "plain.md"
        path.write_text(
            "# 平淡故事\n\n牛牛走到窗边。牛牛说：“今天天气不错。”妈妈点点头，走进厨房。\n",
            encoding="utf-8",
        )
        report = validate_audio_script(compile_audio_script(path, self.facts))
        dialogue = [s for s in compile_audio_script(path, self.facts)["segments"]
                    if s["type"] == "dialogue"]
        self.assertEqual(dialogue[0]["emotion"], DEFAULT_DIALOGUE_EMOTION)
        self.assertTrue(any("神态词" in w for w in report["warnings"]), report["warnings"])

    def test_compilation_is_deterministic(self):
        first = self._script()
        second = self._script()
        self.assertEqual([s["emotion"] for s in first["segments"]],
                         [s["emotion"] for s in second["segments"]])

    def test_report_exposes_emotion_metrics(self):
        report = validate_audio_script(approve_script(self._script()), require_approved=True)
        self.assertTrue(report["ok"], report["errors"])
        metrics = report["metrics"]
        self.assertEqual(metrics["neutral_dialogue_segments"], 0)
        self.assertEqual(metrics["dialogue_emotions"],
                         {"anxious": 1, "awkward": 1, "excited": 1, "laugh": 1, "mystery": 1})


if __name__ == "__main__":
    unittest.main()

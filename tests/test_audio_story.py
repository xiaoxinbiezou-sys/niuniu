from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from pipeline.audio_story import synth_audio_story


class AudioStoryEndingTests(unittest.TestCase):
    def test_fixed_ending_sound_is_appended_to_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "timeline.json"
            output = root / "story.mp3"

            def fake_synth(storyboard, cfg, out_dir, voices_override=None):
                wav_path = Path(out_dir) / "scene_01.wav"
                wav_path.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(wav_path), "wb") as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(44100)
                    stream.writeframes(b"\x00\x00" * 44100)
                return [{"scene_id": 1, "wav": str(wav_path), "duration": 1.0}]

            cfg = {
                "audio_story": {
                    "segment_pause": 0.0,
                    "title_pause": 0.0,
                    "tail_silence": 0.1,
                    "ending_sound": "end_desc4",
                    "ending_sound_volume": 0.45,
                    "bgm_enabled": False,
                },
                "compose": {"voice_eq": "", "max_sfx_volume": 0.6, "bgm_volume": 0.1},
            }
            storyboard = {
                "title": "测试",
                "scenes": [{"id": 1, "segment_id": "s1", "speaker": "旁白",
                             "narration": "故事结束", "title": False}],
            }
            with patch("pipeline.audio_story.tts.synth_scene_audios", side_effect=fake_synth):
                result = synth_audio_story(
                    storyboard, cfg, out_path=str(output), manifest_path=str(manifest)
                )

            self.assertEqual(Path(result), output)
            self.assertTrue(output.is_file())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertGreater(data["total_duration"], 1.0)
            self.assertGreater(data["scenes"][0]["visual_duration"], 1.0)


if __name__ == "__main__":
    unittest.main()

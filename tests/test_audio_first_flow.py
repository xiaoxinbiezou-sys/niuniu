from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from studio.server import (
    EditIn,
    StoryAudioRenderIn,
    VideoIn,
    api_story_audio_confirm,
    api_story_audio_render,
    api_story_edit,
    api_storyboard_generate,
    api_video_create,
)


class AudioFirstFlowTests(unittest.TestCase):
    @patch("studio.server.store.remove_storyboards")
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    def test_story_edit_invalidates_audio_and_visuals(self, get_story, update_story, remove):
        get_story.return_value = {
            "id": "story", "series_id": "series", "idea": "牛牛洗澡",
            "text": "旧正文", "input_type": "idea", "story_type_input": "family",
            "material_type": "idea", "story_type": "family",
        }
        update_story.side_effect = lambda _sid, data: data
        result = api_story_edit("story", EditIn(text="新正文"))
        self.assertEqual(result["audio_status"], "")
        self.assertEqual(result["image_status"], "")
        self.assertEqual(result["audio_file"], "")
        remove.assert_called_once_with("story")

    @patch("studio.server.store.remove_storyboards")
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    def test_whitespace_only_edit_keeps_production(self, get_story, update_story, remove):
        """A trailing newline from the textarea must not destroy a finished audio track."""
        body = "牛牛把种子种进土里，等它发芽。"
        get_story.return_value = {
            "id": "story", "series_id": "series", "idea": "牛牛种种子",
            "text": body, "input_type": "idea", "story_type_input": "family",
            "material_type": "idea", "story_type": "family",
            "audio_status": "ready", "audio_file": "take.mp3",
            "audio_manifest": "take.json", "image_status": "ready",
        }
        update_story.side_effect = lambda _sid, data: data
        for variant in (body + "\n", body + "\r\n", "  " + body + "  "):
            result = api_story_edit("story", EditIn(text=variant))
            self.assertNotIn("audio_status", result, f"wiped production for {variant!r}")
            self.assertNotIn("audio_file", result)
        remove.assert_not_called()

    @patch("studio.server.store.remove_storyboards")
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    def test_real_edit_still_invalidates_and_rehashes(self, get_story, update_story, remove):
        get_story.return_value = {
            "id": "story", "series_id": "series", "idea": "牛牛种种子",
            "text": "旧正文", "input_type": "idea", "story_type_input": "family",
            "material_type": "idea", "story_type": "family",
        }
        update_story.side_effect = lambda _sid, data: data
        result = api_story_edit("story", EditIn(text="牛牛把种子种进土里。\n"))
        self.assertEqual(result["audio_status"], "")
        self.assertEqual(result["audio_file"], "")
        # the stored digest must match the stored text, or /confirm would reject it
        self.assertEqual(result["text"], "牛牛把种子种进土里。")
        self.assertEqual(result["story_digest"],
                         hashlib.sha256("牛牛把种子种进土里。".encode()).hexdigest())
        remove.assert_called_once_with("story")

    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    def test_auto_story_type_does_not_count_as_a_change(self, get_story, update_story):
        """Sending "auto" for a story already resolved to family must not invalidate."""
        get_story.return_value = {
            "id": "story", "series_id": "series", "idea": "牛牛洗澡",
            "text": "牛牛不想洗澡。", "input_type": "idea", "story_type_input": "auto",
            "material_type": "idea", "story_type": "family",
            "audio_status": "ready", "audio_file": "take.mp3",
        }
        update_story.side_effect = lambda _sid, data: data
        result = api_story_edit("story", EditIn(text="牛牛不想洗澡。", story_type="auto"))
        self.assertNotIn("audio_status", result)

    @patch("studio.server.store.get_story")
    def test_visual_plan_requires_listened_audio(self, get_story):
        get_story.return_value = {
            "id": "story", "audio_status": "rendered",
            "audio_file": "audio.mp3", "audio_manifest": "manifest.json",
        }
        with self.assertRaises(HTTPException) as ctx:
            api_storyboard_generate("story")
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("studio.server.store.get_story")
    def test_video_cannot_bypass_audio_stage(self, get_story):
        get_story.return_value = {"id": "story", "audio_status": "rendered", "image_status": "ready"}
        with self.assertRaises(HTTPException) as ctx:
            api_video_create(VideoIn(story_id="story"))
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("studio.server._invalidate_story_production")
    @patch("studio.server.load_config", return_value={"tts": {"doubao": {}, "qwen": {}}})
    @patch("studio.server.store.get_settings")
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    @patch("pipeline.audio_story.synth_approved_audio_script")
    def test_audio_render_waits_for_listen_confirmation(
        self, synth, get_story, update_story, get_settings, _config, _invalidate,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                "story": root / "story.md", "facts": root / "facts.json",
                "script": root / "script.json", "audio": root / "audio.mp3",
                "manifest": root / "manifest.json",
            }
            get_story.return_value = {"id": "story", "audio_status": "approved"}
            get_settings.return_value = {
                "voice": {
                    "active_preset": "preset2",
                    "presets": {"preset2": {"roles": {}}},
                    "providers": {"doubao": {}, "qwen": {}},
                },
            }
            synth.return_value = str(paths["audio"])
            update_story.side_effect = lambda _sid, data: data
            with patch("studio.server._studio_audio_paths", return_value=paths):
                result = api_story_audio_render("story", StoryAudioRenderIn())
            self.assertEqual(result["story"]["audio_status"], "rendered")
            self.assertNotEqual(result["story"]["audio_status"], "ready")

    @patch("pipeline.audio_script.validate_audio_script", return_value={"ok": True, "errors": []})
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.get_story")
    def test_listen_confirmation_checks_digest(self, get_story, update_story, _validate):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "audio.mp3"
            manifest = root / "manifest.json"
            script = root / "script.json"
            audio.write_bytes(b"mp3")
            manifest.write_text(json.dumps({"audio_script_digest": "digest"}), encoding="utf-8")
            script.write_text(json.dumps({"approval": {"digest": "digest"}}), encoding="utf-8")
            get_story.return_value = {
                "id": "story", "audio_status": "rendered", "audio_url": "/media/audio.mp3",
                "audio_file": str(audio), "audio_manifest": str(manifest),
                "audio_script": str(script),
            }
            update_story.side_effect = lambda _sid, data: {**get_story.return_value, **data}
            with patch("studio.server.ROOT", Path("/")):
                result = api_story_audio_confirm("story")
            self.assertEqual(result["story"]["audio_status"], "ready")


if __name__ == "__main__":
    unittest.main()

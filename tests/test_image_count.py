from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from pipeline.visual_plan import build_visual_plan, validate_visual_plan
from studio.server import (
    DEFAULT_IMAGE_COUNT,
    IMAGE_COUNT_CHOICES,
    StoryboardIn,
    api_storyboard_generate,
    resolve_image_count,
)
from studio import store


def _fixture(segment_count: int = 24, seconds: float = 3.7):
    script = {"status": "approved", "title": "牛牛种种子", "approval": {"digest": "d"},
              "segments": []}
    manifest = {"audio_script_digest": "d", "audio_file": "a.mp3", "scenes": [
        {"scene_id": 1, "segment_id": "title", "start": 0, "duration": 3,
         "visual_duration": 3}]}
    cursor = 3.0
    for i in range(segment_count):
        sid = f"s{i + 1}"
        script["segments"].append({"segment_id": sid, "type": "narrator",
                                   "speaker": "旁白", "text": "牛牛给铁罐浇水。"})
        manifest["scenes"].append({"scene_id": i + 2, "segment_id": sid, "start": cursor,
                                   "duration": seconds, "visual_duration": seconds})
        cursor += seconds
    return script, manifest


class ImageCountTests(unittest.TestCase):
    def test_default_is_six(self):
        self.assertEqual(DEFAULT_IMAGE_COUNT, 6)
        self.assertEqual(IMAGE_COUNT_CHOICES, (4, 6, 8, 10))

    @patch("studio.server.store.save_settings")
    @patch("studio.server.store.get_settings", return_value={})
    def test_falls_back_to_default_when_unset(self, _get, _save):
        self.assertEqual(resolve_image_count(None), 6)

    @patch("studio.server.store.save_settings")
    @patch("studio.server.store.get_settings", return_value={"image": {"story_image_count": 8}})
    def test_uses_configured_default(self, _get, _save):
        self.assertEqual(resolve_image_count(None), 8)

    @patch("studio.server.store.get_settings", return_value={"image": {"story_image_count": 99}})
    def test_bogus_configured_value_falls_back(self, _get):
        self.assertEqual(resolve_image_count(None), 6)

    def test_explicit_request_wins(self):
        for value in IMAGE_COUNT_CHOICES:
            self.assertEqual(resolve_image_count(value), value)

    def test_rejects_unsupported_counts(self):
        for value in (0, 5, 7, 11, 100):
            with self.assertRaises(HTTPException) as ctx:
                resolve_image_count(value)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_every_choice_produces_a_valid_plan(self):
        script, manifest = _fixture()
        for choice in IMAGE_COUNT_CHOICES:
            plan = build_visual_plan(script, manifest, max_images=choice)
            self.assertLessEqual(plan["image_count"], choice, f"choice {choice}")
            self.assertEqual(plan["max_images"], choice)
            self.assertTrue(validate_visual_plan(plan, manifest)["ok"])
            covered = [s for scene in plan["scenes"] for s in scene["segment_ids"]]
            self.assertEqual(covered, [r["segment_id"] for r in manifest["scenes"]])

    def test_six_is_reachable_for_a_normal_length_story(self):
        script, manifest = _fixture(segment_count=24, seconds=3.7)
        plan = build_visual_plan(script, manifest, max_images=6)
        self.assertEqual(plan["image_count"], 6)

    def test_plan_passes_its_own_ceiling_to_the_renderer(self):
        script, manifest = _fixture()
        plan = build_visual_plan(script, manifest, max_images=6)
        # api_video_create -> render re-validates with this number
        self.assertEqual(validate_visual_plan(plan, manifest)["image_count"], 6)

    @patch("studio.server._invalidate_story_production")
    @patch("studio.server.store.update_story")
    @patch("studio.server.store.add_storyboard")
    @patch("studio.server.store.get_settings", return_value={"image": {}})
    @patch("studio.server.effective_config")
    @patch("studio.server.store.get_story")
    def test_endpoint_persists_the_requested_count(self, get_story, _cfg, _settings,
                                                   add_sb, update_story, _invalidate):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, manifest = _fixture()
            script_path = root / "audio.json"
            manifest_path = root / "manifest.json"
            audio_path = root / "take.mp3"
            script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            audio_path.write_bytes(b"mp3")
            get_story.return_value = {
                "id": "story", "series_id": "", "status": "confirmed",
                "audio_status": "ready", "audio_script": str(script_path),
                "audio_file": str(audio_path), "audio_manifest": str(manifest_path),
                "story_facts": {"characters": [{"name": "牛牛"}]},
            }
            add_sb.side_effect = lambda sid, data, f: {"id": "sb", "story_id": sid, "data": data}
            with patch("studio.server.ROOT", root):
                rec = api_storyboard_generate("story", StoryboardIn(max_images=4))
            plan = rec["data"]
            self.assertEqual(plan["max_images"], 4)
            self.assertLessEqual(plan["image_count"], 4)
            self.assertEqual(plan["image_count"], len(plan["scenes"]))


if __name__ == "__main__":
    unittest.main()

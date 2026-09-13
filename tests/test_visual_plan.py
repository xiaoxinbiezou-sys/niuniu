from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.render import render_video_from_approved_audio
from pipeline.visual_plan import VisualPlanError, build_visual_plan, validate_visual_plan


def _fixture(segment_count: int = 24) -> tuple[dict, dict]:
    script = {
        "status": "approved",
        "title": "牛牛洗澡",
        "approval": {"digest": "approved-digest"},
        "segments": [],
    }
    manifest = {
        "audio_script_digest": "approved-digest",
        "audio_file": "approved.mp3",
        "scenes": [{
            "scene_id": 1, "segment_id": "title", "start": 0,
            "duration": 1, "visual_duration": 2,
        }],
    }
    cursor = 2.0
    for index in range(segment_count):
        sid = f"seg-{index + 1}"
        text = "牛牛说自己是恐龙。" if index < 8 else "爸爸用水枪追着恐龙喷水。"
        script["segments"].append({
            "segment_id": sid, "type": "narrator", "speaker": "旁白", "text": text,
        })
        manifest["scenes"].append({
            "scene_id": index + 2, "segment_id": sid, "start": cursor,
            "duration": 3.7, "visual_duration": 4.0,
        })
        cursor += 4.0
    return script, manifest


class VisualPlanTests(unittest.TestCase):
    def test_plan_has_at_most_ten_images_and_exact_coverage(self):
        script, manifest = _fixture()
        plan = build_visual_plan(
            script, manifest,
            facts={"characters": [{"name": "牛牛"}, {"name": "爸爸"}]},
            max_images=10,
        )
        self.assertLessEqual(plan["image_count"], 10)
        self.assertTrue(plan["scenes"][0]["title"])
        expected = [row["segment_id"] for row in manifest["scenes"]]
        covered = [sid for scene in plan["scenes"] for sid in scene["segment_ids"]]
        self.assertEqual(covered, expected)
        self.assertEqual(plan["scenes"][-1]["end"], 98.0)

    def test_digest_mismatch_is_rejected(self):
        script, manifest = _fixture(2)
        manifest["audio_script_digest"] = "other"
        with self.assertRaises(VisualPlanError):
            build_visual_plan(script, manifest)

    def test_duplicate_or_reordered_coverage_is_rejected(self):
        script, manifest = _fixture(3)
        plan = build_visual_plan(script, manifest)
        plan["scenes"][-1]["segment_ids"].append("seg-1")
        with self.assertRaises(VisualPlanError):
            validate_visual_plan(plan, manifest)

    @patch("pipeline.render.image_gen.gen_scene_images", side_effect=AssertionError("must not generate"))
    @patch("pipeline.render.compose.compose_video_with_approved_audio", return_value="final.mp4")
    @patch("pipeline.render.subtitles.generate_ass", return_value="subs.ass")
    @patch("pipeline.render.animate.make_scene_clips", return_value=["clip.mp4"])
    def test_render_reuses_images_and_approved_audio(self, _animate, _subs, compose, _image_gen):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "approved.mp3"
            audio.write_bytes(b"mp3")
            image = root / "cover.png"
            image.write_bytes(b"png")
            manifest = {
                "audio_script_digest": "digest",
                "audio_file": str(audio),
                "scenes": [{
                    "scene_id": 1, "segment_id": "title", "start": 0,
                    "duration": 1, "visual_duration": 2,
                }],
            }
            plan = {
                "kind": "visual_plan", "title": "标题", "story_id": "story",
                "audio_script_digest": "digest", "image_count": 1,
                "audio_segments": [{
                    "scene_id": 1, "segment_id": "title", "text": "标题",
                    "duration": 1, "visual_duration": 2,
                }],
                "scenes": [{
                    "id": 1, "start": 0, "end": 2, "duration": 2,
                    "segment_ids": ["title"], "title": True, "narration": "标题",
                }],
                "image_files": [str(image)],
            }
            plan_path = root / "plan.json"
            manifest_path = root / "manifest.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            cfg = {
                "project": {"width": 1080, "height": 1920},
                "subtitle": {}, "compose": {}, "image": {},
            }
            result = render_video_from_approved_audio(
                str(plan_path), cfg, str(audio), str(manifest_path), out="final.mp4",
            )
            self.assertEqual(result, "final.mp4")
            self.assertEqual(compose.call_args.args[2], str(audio))


if __name__ == "__main__":
    unittest.main()

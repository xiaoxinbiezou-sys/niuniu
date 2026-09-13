"""删除接口与批量出片编排的测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from studio import server, store


class DeleteEndpointTests(unittest.TestCase):
    def test_delete_missing_video_is_404(self):
        with patch("studio.server.store.get_video", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                server.api_video_delete("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_missing_story_is_404(self):
        with patch("studio.server.store.get_story", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                server.api_story_delete("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_deleting_a_video_removes_its_file_and_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mp4 = root / "output" / "videos" / "v1.mp4"
            mp4.parent.mkdir(parents=True)
            mp4.write_bytes(b"x")
            video = {"id": "v1", "story_id": "s1", "status": "ready",
                     "mp4": "output/videos/v1.mp4", "cover": ""}
            with patch("studio.server.ROOT", root), \
                    patch("studio.server.store.get_video", return_value=video), \
                    patch("studio.server.store.remove_video") as remove_video, \
                    patch("studio.server.store.list_jobs", return_value=[
                        {"id": "j1", "video_id": "v1"}]), \
                    patch("studio.server.store.remove_job") as remove_job:
                result = server._delete_video("v1")
            self.assertTrue(result["ok"])
            self.assertFalse(mp4.is_file(), "MP4 应该被删掉")
            remove_video.assert_called_once_with("v1")
            remove_job.assert_called_once_with("j1")

    def test_deleting_a_story_removes_its_artifacts_and_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story_files = {}
            for name in ("story", "facts", "script", "manifest", "audio"):
                p = root / "stories" / "studio" / f"{name}.s1"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
                story_files[name] = p
            image = root / "output" / "images" / "studio_s1_abc" / "group_00.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"x")
            sb_file = root / "stories" / "storyboard.x.json"
            sb_file.write_text("{}", encoding="utf-8")
            record = {"id": "sb1", "story_id": "s1", "file": str(sb_file),
                      "data": {"image_files": [str(image)]}}

            with patch("studio.server.ROOT", root), \
                    patch("studio.server.store.get_story", return_value={"id": "s1"}), \
                    patch("studio.server.store.list_videos",
                          return_value=[{"id": "v1", "story_id": "s1"}]), \
                    patch("studio.server._delete_video") as del_video, \
                    patch("studio.server.store.list_storyboards", return_value=[record]), \
                    patch("studio.server.store.remove_storyboards") as rm_sb, \
                    patch("studio.server.store.remove_story") as rm_story, \
                    patch("studio.server._studio_audio_paths", return_value=story_files):
                result = server.api_story_delete("s1")

            self.assertTrue(result["ok"])
            del_video.assert_called_once_with("v1")
            rm_sb.assert_called_once_with("s1")
            rm_story.assert_called_once_with("s1")
            self.assertFalse(image.parent.is_dir(), "配图目录应被删掉")
            self.assertFalse(sb_file.is_file(), "配图方案文件应被删掉")
            for name, p in story_files.items():
                self.assertFalse(p.is_file(), f"{name} 应被删掉")


class BatchTests(unittest.TestCase):
    def test_empty_ideas_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server.api_batch_create(server.BatchIn2(ideas=["  ", ""]))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_too_many_ideas_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            server.api_batch_create(server.BatchIn2(ideas=[f"点子{i}" for i in range(21)]))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("20", str(ctx.exception.detail))

    def test_missing_voice_preset_rejected(self):
        with patch("studio.server.store.get_series", return_value={"id": "s"}), \
                patch("studio.server.resolve_image_count", return_value=6), \
                patch("studio.server.store.get_settings",
                      return_value={"voice": {"presets": {}, "providers": {}}}):
            with self.assertRaises(HTTPException) as ctx:
                server.api_batch_create(server.BatchIn2(ideas=["牛牛洗澡"], series_id="s"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("声音", str(ctx.exception.detail))

    def test_missing_image_credential_rejected(self):
        settings = {"voice": {"presets": {"preset2": {"roles": {"旁白": {}}}},
                              "providers": {"doubao": {"x_api_key": "k"}}}}
        with patch("studio.server.store.get_series", return_value={"id": "s"}), \
                patch("studio.server.resolve_image_count", return_value=6), \
                patch("studio.server.store.get_settings", return_value=settings), \
                patch("studio.server.image_provider_cfg", return_value={"api_key": ""}):
            with self.assertRaises(HTTPException) as ctx:
                server.api_batch_create(server.BatchIn2(ideas=["牛牛洗澡"], series_id="s"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("文生图", str(ctx.exception.detail))

    def test_batch_runs_every_step_and_marks_done(self):
        batch = {"id": "b1", "series_id": "s1", "image_count": 6, "story_type": "auto",
                 "status": "queued",
                 "items": [{"index": 0, "idea": "牛牛洗澡", "status": "pending",
                            "stage": "", "story_id": "", "video_id": "", "title": "",
                            "error": ""}]}
        calls = []

        def fake_step(name):
            return lambda *a, **k: calls.append(name)

        with patch("studio.server.series_meta",
                   return_value={"cover_template": "cartoon"}), \
                patch("studio.server.store.update_batch"), \
                patch("studio.server.store.update_batch_item"), \
                patch("studio.server.store.get_batch", return_value=batch), \
                patch("studio.server._batch_generate_story",
                      side_effect=lambda *a: {"id": "st1", "title": "牛牛洗澡"}), \
                patch("studio.server._confirm_story", side_effect=fake_step("confirm")), \
                patch("studio.server._generate_audio_script", side_effect=fake_step("script")), \
                patch("studio.server._approve_audio_script", side_effect=fake_step("approve")), \
                patch("studio.server._render_story_audio", side_effect=fake_step("audio")), \
                patch("studio.server._confirm_story_audio", side_effect=fake_step("listen")), \
                patch("studio.server._build_storyboard", side_effect=fake_step("plan")), \
                patch("studio.server._generate_images_for_story", side_effect=fake_step("images")), \
                patch("studio.server._create_video",
                      side_effect=lambda *a, **k: (calls.append("video"),
                                                   {"video": {"id": "v1"}})[1]), \
                patch("studio.server.store.get_video", return_value={"status": "ready"}), \
                patch("studio.server.time.sleep"):
            server._run_batch(batch)

        self.assertEqual(calls, ["confirm", "script", "approve", "audio", "listen",
                                 "plan", "images", "video"])

    def test_batch_records_every_stage_in_the_timeline(self):
        """每一步都要留下流水记录，前端才能显示"故事/脚本/音频/配图/成片"这些节点。"""
        batch = {"id": "b1", "series_id": "s1", "image_count": 6, "story_type": "auto",
                 "items": [{"index": 0, "idea": "牛牛洗澡", "status": "pending", "stage": "",
                            "stages": [], "story_id": "", "video_id": "", "title": "",
                            "error": ""}]}
        recorded = []

        with patch("studio.server.series_meta",
                   return_value={"cover_template": "cartoon"}), \
                patch("studio.server.store.update_batch"), \
                patch("studio.server.store.update_batch_item"), \
                patch("studio.server.store.append_batch_stage",
                      side_effect=lambda bid, idx, name, status, detail="": recorded.append(
                          (name, status))), \
                patch("studio.server.store.get_batch", return_value=batch), \
                patch("studio.server._batch_generate_story",
                      return_value={"id": "st1", "title": "牛牛洗澡"}), \
                patch("studio.server._confirm_story"), \
                patch("studio.server._generate_audio_script"), \
                patch("studio.server._approve_audio_script"), \
                patch("studio.server._render_story_audio"), \
                patch("studio.server._confirm_story_audio"), \
                patch("studio.server._build_storyboard"), \
                patch("studio.server._generate_images_for_story"), \
                patch("studio.server._create_video", return_value={"video": {"id": "v1"}}), \
                patch("studio.server.store.get_video", return_value={"status": "ready"}), \
                patch("studio.server.time.sleep"):
            server._run_batch(batch)

        done = [name for name, status in recorded if status == "done"]
        # 编译和批准合并成一个节点：质量门是在"批准"时报错的，分开会让重试失去作用域
        for expected in ("写故事", "确认母稿", "编译并批准脚本", "合成音频",
                         "确认音频", "规划配图", "生成配图", "渲染成片"):
            self.assertIn(expected, done, f"缺少阶段流水：{expected}")

    def test_script_failure_triggers_story_rewrite(self):
        """脚本因"台词缺引导/占比过低"失败时，要重写故事再试，而不是直接判死。"""
        batch = {"id": "b1", "series_id": "s1", "image_count": 6, "story_type": "auto",
                 "items": [{"index": 0, "idea": "牛牛洗澡", "status": "pending", "stage": "",
                            "stages": [], "story_id": "", "video_id": "", "title": "",
                            "error": ""}]}
        attempts = {"script": 0, "rewrites": 0}

        def script(*a):
            attempts["script"] += 1
            if attempts["script"] == 1:
                raise HTTPException(400, "音频脚本质量门未通过:\n"
                                         "- seg_x: 这句台词前面没有旁白引导")

        def rewrite(*a, **k):
            attempts["rewrites"] += 1
            return {"title": "改好的故事", "text": "正文", "story_digest": "d",
                    "story_facts": {}}

        with patch("studio.server.series_meta",
                   return_value={"cover_template": "cartoon"}), \
                patch("studio.server.store.update_batch"), \
                patch("studio.server.store.update_batch_item"), \
                patch("studio.server.store.append_batch_stage"), \
                patch("studio.server.store.update_story"), \
                patch("studio.server.store.get_story", return_value={"text": "旧正文",
                                                                     "story_type": "family"}), \
                patch("studio.server.store.get_batch", return_value=batch), \
                patch("studio.server._batch_generate_story",
                      return_value={"id": "st1", "title": "牛牛洗澡"}), \
                patch("studio.server._confirm_story"), \
                patch("studio.server._generate_audio_script", side_effect=script), \
                patch("studio.server.story_gen.generate_story_package", side_effect=rewrite), \
                patch("studio.server._approve_audio_script"), \
                patch("studio.server._render_story_audio"), \
                patch("studio.server._confirm_story_audio"), \
                patch("studio.server._build_storyboard"), \
                patch("studio.server._generate_images_for_story"), \
                patch("studio.server._create_video", return_value={"video": {"id": "v1"}}), \
                patch("studio.server.store.get_video", return_value={"status": "ready"}), \
                patch("studio.server.time.sleep"):
            server._run_batch(batch)

        self.assertEqual(attempts["rewrites"], 1, "应该重写一次故事")
        self.assertEqual(attempts["script"], 2, "重写后应该再编译一次脚本")

    def test_one_bad_idea_does_not_stop_the_batch(self):
        batch = {"id": "b1", "series_id": "s1", "image_count": 6, "story_type": "auto",
                 "items": [
                     {"index": 0, "idea": "坏点子", "status": "pending", "stage": "",
                      "story_id": "", "video_id": "", "title": "", "error": ""},
                     {"index": 1, "idea": "好点子", "status": "pending", "stage": "",
                      "story_id": "", "video_id": "", "title": "", "error": ""},
                 ]}
        marks = []

        def gen(item, *a):
            if item["idea"] == "坏点子":
                raise HTTPException(422, "故事未通过质检")
            return {"id": "st2", "title": "好故事"}

        with patch("studio.server.series_meta",
                   return_value={"cover_template": "cartoon"}), \
                patch("studio.server.store.update_batch"), \
                patch("studio.server.store.update_batch_item",
                      side_effect=lambda bid, idx, patch: marks.append((idx, patch.get("status")))), \
                patch("studio.server.store.get_batch", return_value=batch), \
                patch("studio.server._batch_generate_story", side_effect=gen), \
                patch("studio.server._confirm_story"), \
                patch("studio.server._generate_audio_script"), \
                patch("studio.server._approve_audio_script"), \
                patch("studio.server._render_story_audio"), \
                patch("studio.server._confirm_story_audio"), \
                patch("studio.server._build_storyboard"), \
                patch("studio.server._generate_images_for_story"), \
                patch("studio.server._create_video", return_value={"video": {"id": "v2"}}), \
                patch("studio.server.store.get_video", return_value={"status": "ready"}), \
                patch("studio.server.time.sleep"):
            server._run_batch(batch)

        failed = [m for m in marks if m[1] == "failed"]
        done = [m for m in marks if m[1] == "done"]
        self.assertEqual(len(failed), 1, f"第一条应该失败: {marks}")
        self.assertEqual(len(done), 1, f"第二条应该成功: {marks}")
        self.assertEqual(failed[0][0], 0)
        self.assertEqual(done[0][0], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from studio.server import _recover_interrupted_jobs


class InterruptedJobRecoveryTests(unittest.TestCase):
    """队列是串行的：一个 job 永远停在 running，后面排队的就全卡死。"""

    def _store(self, jobs, videos):
        return (
            patch("studio.server.store.list_jobs", return_value=jobs),
            patch("studio.server.store.update_job"),
            patch("studio.server.store.get_video",
                  side_effect=lambda vid: next((v for v in videos if v["id"] == vid), None)),
            patch("studio.server.store.update_video"),
        )

    def test_running_job_is_marked_failed_on_startup(self):
        jobs = [{"id": "j1", "video_id": "v1", "status": "running", "log": "开始"},
                {"id": "j2", "video_id": "v2", "status": "done", "log": ""},
                {"id": "j3", "video_id": "v3", "status": "queued", "log": ""}]
        videos = [{"id": "v1", "status": "rendering"}, {"id": "v2", "status": "ready"},
                  {"id": "v3", "status": "queued"}]
        p_jobs, p_upd_job, p_get_vid, p_upd_vid = self._store(jobs, videos)
        with p_jobs, p_upd_job as upd_job, p_get_vid, p_upd_vid as upd_vid:
            count = _recover_interrupted_jobs()
        self.assertEqual(count, 1)
        upd_job.assert_called_once()
        args = upd_job.call_args.args
        self.assertEqual(args[0], "j1")
        self.assertEqual(args[1]["status"], "failed")
        self.assertIn("中断", args[1]["log"])
        upd_vid.assert_called_once_with("v1", {"status": "failed"})

    def test_queued_jobs_are_left_alone(self):
        jobs = [{"id": "j1", "video_id": "v1", "status": "queued", "log": ""}]
        videos = [{"id": "v1", "status": "queued"}]
        p_jobs, p_upd_job, p_get_vid, p_upd_vid = self._store(jobs, videos)
        with p_jobs, p_upd_job as upd_job, p_get_vid, p_upd_vid as upd_vid:
            count = _recover_interrupted_jobs()
        self.assertEqual(count, 0)
        upd_job.assert_not_called()
        upd_vid.assert_not_called()

    def test_video_not_in_rendering_is_untouched(self):
        jobs = [{"id": "j1", "video_id": "v1", "status": "running", "log": ""}]
        videos = [{"id": "v1", "status": "ready"}]
        p_jobs, p_upd_job, p_get_vid, p_upd_vid = self._store(jobs, videos)
        with p_jobs, p_upd_job as upd_job, p_get_vid, p_upd_vid as upd_vid:
            count = _recover_interrupted_jobs()
        self.assertEqual(count, 1)
        upd_job.assert_called_once()
        upd_vid.assert_not_called()


if __name__ == "__main__":
    unittest.main()

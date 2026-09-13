from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline import qa


def _images() -> tuple[str, str]:
    temp = tempfile.mkdtemp()
    ref = Path(temp) / "ref.png"
    img = Path(temp) / "img.png"
    ref.write_bytes(b"ref")
    img.write_bytes(b"img")
    return str(ref), str(img)


def _response(content: str):
    """Fake urlopen() context manager returning a chat-completions payload."""
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    return _Resp()


class VerdictParsingTests(unittest.TestCase):
    def test_answer_is_parsed_as_json_not_by_substring(self):
        # 旧的判定式 `"一致:是" in ans or "是" in ans.split()[0]` 会把"否"判成通过
        self.assertEqual(qa._parse_verdict('{"consistent":false,"faithful":true}')[:2],
                         ("inconsistent", "consistent"))
        self.assertEqual(qa._parse_verdict('{"consistent":true,"faithful":false}')[:2],
                         ("consistent", "inconsistent"))
        self.assertEqual(qa._parse_verdict('{"consistent":true,"faithful":true}')[:2],
                         ("consistent", "consistent"))

    def test_json_wrapped_in_prose_or_code_fence(self):
        ans = '好的```json\n{"consistent": false, "faithful": true, "reason": "脸型不同"}\n```'
        self.assertEqual(qa._parse_verdict(ans)[:2], ("inconsistent", "consistent"))

    def test_plain_text_fallback_requires_explicit_negative(self):
        self.assertEqual(qa._parse_verdict("一致:是")[:2], ("consistent", "unknown"))
        self.assertEqual(qa._parse_verdict("一致:否")[:2], ("inconsistent", "unknown"))

    def test_garbage_answer_is_unknown_not_pass(self):
        """读不懂时不能当通过放行，否则等于没有质检。"""
        self.assertEqual(qa._parse_verdict("")[:2], ("unknown", "unknown"))
        self.assertEqual(qa._parse_verdict("嗯……让我想想")[:2], ("unknown", "unknown"))

    def test_unreadable_model_output_is_unclear_not_checked(self):
        ref, img = _images()
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response("嗯……让我想想")):
            verdict = qa.check_image([ref], img, {})
        self.assertEqual(verdict["status"], qa.UNCLEAR)
        self.assertFalse(verdict["ok"])

    def test_contradiction_is_reported_as_a_warning_when_it_names_the_object(self):
        ref, img = _images()
        vague = ('{"consistent":true,"faithful":false,"contradiction":"",'
                 '"reason":"孩子表情与旁白描述的情绪不完全一致"}')
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response(vague)):
            verdict = qa.check_image([ref], img, {}, narration="牛牛瞪大眼睛，然后咯咯咯地笑起来")
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(verdict["warnings"])

    def test_named_contradiction_appears_in_warnings(self):
        ref, img = _images()
        concrete = ('{"consistent":true,"faithful":false,"contradiction":"已经长出的草莓",'
                    '"reason":"旁白说里面只有泥土"}')
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response(concrete)):
            verdict = qa.check_image([ref], img, {}, narration="打开一看，里面只有泥土")
        self.assertTrue(verdict["ok"])
        self.assertIn("草莓", "".join(verdict["warnings"]))


class CheckImageTests(unittest.TestCase):
    def test_missing_key_is_unavailable_not_failure(self):
        ref, img = _images()
        with patch("pipeline.qa._qa_key", return_value=""):
            verdict = qa.check_image([ref], img, {})
        self.assertEqual(verdict["status"], qa.UNAVAILABLE)
        self.assertFalse(verdict["ok"])

    def test_network_error_is_unavailable_not_failure(self):
        ref, img = _images()
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no net")):
            verdict = qa.check_image([ref], img, {})
        self.assertEqual(verdict["status"], qa.UNAVAILABLE)
        self.assertIn("调用失败", verdict["reason"])

    def test_inconsistency_is_a_checked_failure(self):
        ref, img = _images()
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen",
                      return_value=_response('{"consistent":false,"faithful":true,"reason":"脸型不同"}')):
            verdict = qa.check_image([ref], img, {})
        self.assertEqual(verdict["status"], qa.CHECKED)
        self.assertFalse(verdict["ok"])
        self.assertIn("定妆照", verdict["reason"])

    def test_contradiction_with_narration_becomes_a_warning_not_a_block(self):
        """视觉模型对"此刻该不该出现"的判断会抖动，拿它拦整条流水线会让人出不了片，
        所以事实矛盾只报警告；角色不一致仍然是硬失败（见下一条）。"""
        ref, img = _images()
        answer = ('{"consistent":true,"faithful":false,"contradiction":"已经长出的草莓",'
                  '"reason":"旁白说里面只有泥土"}')
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response(answer)):
            verdict = qa.check_image([ref], img, {}, narration="打开一看，里面只有泥土，别的什么都没有")
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(any("草莓" in w for w in verdict["warnings"]))

    def test_character_mismatch_still_blocks(self):
        ref, img = _images()
        answer = '{"consistent":false,"faithful":true,"reason":"脸型与定妆照不同"}'
        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response(answer)):
            verdict = qa.check_image([ref], img, {}, narration="牛牛在客厅里玩")
        self.assertFalse(verdict["ok"])
        self.assertIn("定妆照", verdict["reason"])

    def test_narration_is_included_in_the_prompt(self):
        ref, img = _images()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _response('{"consistent":true,"faithful":true,"reason":"ok"}')

        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", fake_urlopen):
            qa.check_image([ref], img, {}, narration="牛牛围着铁罐转了八圈")
        text = " ".join(p.get("text", "") for p in captured["body"]["messages"][0]["content"])
        self.assertIn("牛牛围着铁罐转了八圈", text)

    def test_missing_image_is_a_failure(self):
        ref, _ = _images()
        with patch("pipeline.qa._qa_key", return_value="k"):
            verdict = qa.check_image([ref], "/nonexistent/img.png", {})
        self.assertEqual(verdict["status"], qa.CHECKED)
        self.assertFalse(verdict["ok"])


class StrictRetryTests(unittest.TestCase):
    def test_redraws_until_it_passes(self):
        ref, img = _images()
        answers = iter([
            '{"consistent":false,"faithful":true}',
            '{"consistent":true,"faithful":true}',
        ])
        redraws = []

        def gen(scene, out):
            redraws.append(out)
            Path(out).write_bytes(b"new")

        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", side_effect=lambda *a, **k: _response(next(answers))):
            ok, tries, reason = qa.check_image_strict([ref], img, {}, max_attempts=3,
                                                      gen_fn=gen, scene={})
        self.assertTrue(ok)
        self.assertEqual(tries, 2)
        self.assertEqual(len(redraws), 1)

    def test_no_redraw_when_qa_is_unavailable(self):
        """没 key 时不能反复重画（旧行为：重画 3 次再报"连续三次不一致"）。"""
        ref, img = _images()
        redraws = []

        def gen(scene, out):
            redraws.append(out)

        with patch("pipeline.qa._qa_key", return_value=""):
            ok, tries, reason = qa.check_image_strict([ref], img, {}, max_attempts=3,
                                                      gen_fn=gen, scene={})
        self.assertFalse(ok)
        self.assertEqual(len(redraws), 0)
        self.assertIn("质检 key", reason)

    def test_gives_up_after_max_attempts_with_a_reason(self):
        ref, img = _images()
        redraws = []

        def gen(scene, out):
            redraws.append(out)
            Path(out).write_bytes(b"new")

        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen",
                      return_value=_response('{"consistent":false,"faithful":false}')):
            ok, tries, reason = qa.check_image_strict([ref], img, {}, max_attempts=3,
                                                      gen_fn=gen, scene={})
        self.assertFalse(ok)
        self.assertEqual(tries, 3)
        self.assertEqual(len(redraws), 2)   # 重画在每次失败后，最后一次不再画
        self.assertTrue(reason)

    def test_redraws_are_not_counted_for_unclear_answers(self):
        ref, img = _images()
        redraws = []

        def gen(scene, out):
            redraws.append(out)

        with patch("pipeline.qa._qa_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_response("我不确定")):
            ok, tries, reason = qa.check_image_strict([ref], img, {}, max_attempts=3,
                                                      gen_fn=gen, scene={})
        self.assertFalse(ok)
        self.assertEqual(len(redraws), 0)


if __name__ == "__main__":
    unittest.main()

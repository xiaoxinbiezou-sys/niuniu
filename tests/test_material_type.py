from __future__ import annotations

import unittest

from pipeline.story_gen import detect_material_type, resolve_material_type


class MaterialTypeTests(unittest.TestCase):
    def test_auto_detects_short_prompt_as_idea(self):
        self.assertEqual(detect_material_type("牛牛梦见自己变成了巨人"), "idea")

    def test_auto_detects_ellipsis_as_incomplete(self):
        text = "牛牛走进森林以后发现一扇发光的小门，他伸手推开，里面传来奇怪的声音……"
        self.assertEqual(detect_material_type(text), "incomplete")

    def test_auto_detects_complete_story(self):
        text = "牛牛一直想长大。一天他来到小人国，发现自己吃多少都吃不饱。醒来后他扑进妈妈怀里。妈妈听完笑了。"
        self.assertEqual(detect_material_type(text), "complete")

    def test_explicit_selection_overrides_heuristic(self):
        long_text = "这是一个已经写了很多字但用户明确说明只是点子的输入。" * 8
        self.assertEqual(resolve_material_type("idea", long_text), "idea")
        self.assertEqual(resolve_material_type("incomplete", "一句话。"), "incomplete")
        self.assertEqual(resolve_material_type("complete", "一个点子"), "complete")

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_material_type("guess", "测试")


if __name__ == "__main__":
    unittest.main()

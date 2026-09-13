"""Build and validate a compact visual plan around an approved audio story."""
from __future__ import annotations

import json
import re
from pathlib import Path

# 项目根目录：脚本里记录的 story_path 是相对它写的
ROOT = Path(__file__).resolve().parent.parent


class VisualPlanError(ValueError):
    pass


SCENE_RULES = (
    ("家里卫生间", ("洗澡", "澡盆", "浴缸", "浴室", "卫生间", "淋浴")),
    ("家里卧室", ("床", "枕头", "被子", "睡觉", "卧室")),
    ("家里厨房", ("厨房", "锅", "做饭", "冰箱", "餐桌")),
    ("小区公园", ("公园", "草地", "水坑", "滑梯", "秋千")),
    ("超市", ("超市", "货架", "购物车")),
    ("幼儿园", ("幼儿园", "老师", "教室")),
)

QUOTE_CHARS = '"“”「」『』'

# 去掉台词后可能剩下的引导语残片，出现就说明画面其实在前半句
_ACTION_DANGLING = ("嘴里", "心里", "然后", "接着", "于是", "就", "又", "着", "地", "的")


def _read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _scene_name(text: str, fallback: str = "家里客厅") -> str:
    for name, words in SCENE_RULES:
        if any(word in text for word in words):
            return name
    return fallback


def _character_names(facts: dict) -> list[str]:
    names = []
    for row in facts.get("characters") or []:
        name = row if isinstance(row, str) else row.get("name", "")
        if name and name != "旁白":
            names.append(name)
    return names


def _partition(rows: list[dict], count: int) -> list[list[dict]]:
    """Partition contiguous audio rows, preferring location and action boundaries."""
    if not rows:
        return []
    count = max(1, min(count, len(rows)))
    if count == 1:
        return [rows]
    total = sum(float(row["visual_duration"]) for row in rows)
    groups: list[list[dict]] = []
    current: list[dict] = []
    elapsed = 0.0
    previous_scene = _scene_name(rows[0].get("text", ""))
    for index, row in enumerate(rows):
        remaining_rows = len(rows) - index
        remaining_groups = count - len(groups)
        target = max((total - elapsed) / remaining_groups, 0.1)
        row_scene = _scene_name(row.get("text", ""), previous_scene)
        current_duration = sum(float(item["visual_duration"]) for item in current)
        location_break = bool(current and row_scene != previous_scene)
        duration_break = bool(current and current_duration >= target * 0.86)
        must_leave_rows = remaining_rows == remaining_groups
        if len(groups) < count - 1 and (location_break or duration_break or must_leave_rows):
            groups.append(current)
            elapsed += current_duration
            current = []
        current.append(row)
        previous_scene = row_scene
    if current:
        groups.append(current)
    while len(groups) > count:
        tail = groups.pop()
        groups[-1].extend(tail)
    return groups


# 角色名 → 画面里的固定称呼（避免"牛牛"被当成要画两个）
_CHARACTER_NOUNS = {
    "牛牛": "小男孩", "添添": "小女孩", "爸爸": "爸爸", "妈妈": "妈妈",
}
_CHARACTER_ORDER = ("牛牛", "添添", "爸爸", "妈妈")

# 反复出现的道具必须给出**固定外观**，否则每张图都会自己重新猜一个尺寸和造型。
# 键是旁白里会出现的说法，值是统一的画面定义。尺寸一律用"相对人物"描述：
# 只写绝对厘米数时，模型会把道具放在镜头前拍成巨大一个。
_PROP_SPECS = (
    ("铁罐", "银色金属小茶叶罐，很小，只有小男孩手掌那么大（大约到他的小腿一半高），"
             "侧面光滑没有商标，全部画面里大小完全一样"),
    ("浇水壶", "蓝绿色儿童塑料浇水壶，只有小男孩手掌那么大，壶嘴细长，全部画面大小完全一样"),
    ("草莓种子", "小小的深褐色草莓种子，装在透明小塑料袋里，颗粒像芝麻一样小"),
    ("小绿芽", "刚从土里钻出的两瓣嫩绿小芽，只有几厘米高，叶片圆润"),
)


def _strip_quotes(text: str, dialogue: tuple[str, ...] = ()) -> str:
    """去掉台词，连带它前面的引导语。

    台词是拿去配音和烧字幕的，写进生图描述会被模型**当成画面文字画在图上**
    （实测出现过整句字幕糊在画面中央）。

    音频脚本里台词已经不带引号了（编译时被剥掉），所以光按引号删不干净；调用方会把
    这一拍覆盖到的 dialogue 段原文传进来，按原文精确删除。
    """
    cleaned = text or ""
    for line in sorted(dialogue, key=len, reverse=True):
        if line:
            cleaned = cleaned.replace(line, "")
    # 带引号的台词（母稿风格）连引导语一起删
    cleaned = re.sub(r"(说|问|喊|叫|回答|答|嚷|念叨|嘟囔|道)[^，,、。；;：:]{0,4}\s*[：:]"
                     rf"\s*[{re.escape(QUOTE_CHARS)}][^{re.escape(QUOTE_CHARS)}]*"
                     rf"[{re.escape(QUOTE_CHARS)}]", "", cleaned)
    cleaned = re.sub(rf"[{re.escape(QUOTE_CHARS)}][^{re.escape(QUOTE_CHARS)}]*"
                     rf"[{re.escape(QUOTE_CHARS)}]", "", cleaned)
    # 台词被切在下一个节拍时，这里只剩一个悬空的引导语（"……，嘴里念叨："）。
    # 反复剥，能处理"嘴里念叨："这种叠起来的写法。
    guide = r"(?:嘴里|心里|手里|眼中|脸上|回头|转身)?(?:说|问|喊|叫|回答|答|嚷|念叨|嘟囔|道)[^，,、。；;：:]{0,4}\s*[：:]"
    while re.search(guide + r"\s*$", cleaned):
        cleaned = re.sub(guide + r"\s*$", "", cleaned)
    cleaned = re.sub(r"[，,、；;]{2,}", "，", cleaned)
    return cleaned.strip("，,、；;。：: \t")


def _character_clause(present: list[str], tail: bool = False) -> str:
    """"画面里有且只有 1 个小男孩（牛牛）、1 个爸爸…" — 逐个写明人数。

    旧写法"图中只出现牛牛"会被理解成"要画牛牛"；而只写"共有 4 个人物"也不够——
    实测会画出两个爸爸。必须把每个角色各几个写死，并在描述末尾再强调一次。
    """
    if not present:
        return "画面里没有人物，只有环境和道具"
    ordered = [name for name in _CHARACTER_ORDER if name in present]
    ordered += [name for name in present if name not in _CHARACTER_ORDER]
    if tail:
        counts = "、".join(
            f"1个{_CHARACTER_NOUNS.get(name, name)}" if name in _CHARACTER_NOUNS else f"1个{name}"
            for name in ordered)
        return f"【人物数量不许变】画面里只能有：{counts}，一个都不能多画"
    people = "、".join(
        f"1 个{_CHARACTER_NOUNS.get(name, name)}（{name}）" if name in _CHARACTER_NOUNS
        else f"1 个{name}"
        for name in ordered
    )
    return (f"画面里共有且仅有 {len(ordered)} 个人物：{people}。"
            f"每个角色都只能出现一次，同一个人不要画两遍")


def _prop_clause(text: str) -> str:
    """本镜出现的道具，用全片统一的定义写死，压住跨图尺寸漂移。"""
    specs = [spec for key, spec in _PROP_SPECS if key in text]
    if not specs:
        return ""
    return "本镜道具（与前后画面完全一致的同一件，尺寸不得改变）：" + "；".join(specs) + "。"


def _action_clause(text: str, dialogue: tuple[str, ...] = (), limit: int = 60) -> str:
    """这一镜要画的那一个瞬间。

    顺序很关键：**先删台词 → 再剥掉句尾的引导语 → 最后取一句**。
    先取句会把带引导语的从句选中（"……嘴里念叨"），画面反而丢了动作；
    剥干净之后最后一句通常正是"牛牛围着铁罐转了八圈"这类可画的动作。

    一个视觉节拍可能合并了好几段旁白，整段抄进提示词会让模型什么都想画（历史上出现过
    "桶不见了、人物重复"），所以只保留一句，过长时截到自然停顿。
    """
    cleaned = _strip_quotes(text, dialogue).strip()

    def sentences(source: str) -> list[str]:
        return [p.strip() for p in re.split(r"[。！？!?\n]", source) if p.strip()]

    def trim(chosen: str) -> str:
        if len(chosen) > limit:
            cut = max(chosen.rfind("，"), chosen.rfind("、"), chosen.rfind("；"))
            chosen = chosen[:cut] if cut >= limit // 2 else chosen[:limit]
        return chosen.rstrip("，,、；;。：: \t")

    for sentence in reversed(sentences(cleaned)):
        chosen = trim(sentence)
        if chosen and not chosen.endswith(_ACTION_DANGLING):
            return chosen
    return trim(sentences(cleaned)[-1]) if sentences(cleaned) else ""


def _prompt(scene: str, text: str, present: list[str], facts: dict,
            dialogue: tuple[str, ...] = (), scene_base: str = "") -> str:
    fact_rules = "；".join(str(item) for item in facts.get("world_facts") or [])
    scale = (f"世界尺度硬规则：{fact_rules}。"
             if fact_rules else
             "所有人、家具和道具保持现实且统一的尺寸关系：道具相对人物的比例在每一张画面里都相同。")
    action = _action_clause(text, dialogue)
    # 场景由模型逐镜判断（见 audio_annotator.plan_scenes）。它可能给出户外地点
    # （"小区花坛边"），所以不能再按"家里"两个字决定道具归属。
    place = "室内" if any(w in scene for w in ("家", "室", "厅", "厨", "房", "床", "校", "园", "店")) else "原地"
    # 环境基准卡：同一地点的多镜逐字复用同一段环境描写，跨镜才一致
    base = f"环境固定为：{scene_base}。" if scene_base else ""
    return (
        f"儿童家庭故事插画，场景固定为{scene}。{base}"
        f"{_character_clause(present)}。"
        f"只表现这一个瞬间：{action}。"
        f"{_prop_clause(text)}"
        f"{scale}"
        f"同一人物的长相、发型、服装与前后画面完全一致，道具留在{place}里，"
        f"禁止增加旁白没有提到的人物，画面里不要出现任何文字。"
        f"{_character_clause(present, tail=True)}。"
    )


def build_visual_plan(script: dict, manifest: dict, facts: dict | None = None,
                      cast: dict | None = None, max_images: int = 10,
                      llm_cfg: dict | None = None) -> dict:
    """Merge the exact approved audio timeline into at most ``max_images`` beats.

    ``llm_cfg`` 给了就用模型逐镜判断地点与环境；不给（或调用失败）退回关键词规则。
    """
    facts = facts or {}
    max_images = max(1, min(int(max_images), 10))
    digest = (script.get("approval") or {}).get("digest", "")
    if not digest or script.get("status") != "approved":
        raise VisualPlanError("只能为已批准的音频脚本规划画面")
    if manifest.get("audio_script_digest") != digest:
        raise VisualPlanError("音频时间轴与已批准音频脚本摘要不一致")

    script_segments = {row["segment_id"]: row for row in script.get("segments") or []}
    timeline = []
    for index, row in enumerate(manifest.get("scenes") or []):
        segment_id = str(row.get("segment_id") or "")
        if segment_id == "title":
            source = {"speaker": "旁白", "text": script.get("title", ""), "type": "narrator"}
        else:
            source = script_segments.get(segment_id)
        if not source:
            raise VisualPlanError(f"音频时间轴包含未知段落：{segment_id}")
        timeline.append({
            "scene_id": int(row.get("scene_id", index + 1)),
            "segment_id": segment_id,
            "speaker": source.get("speaker", "旁白"),
            "type": source.get("type", "narrator"),
            "text": source.get("text", ""),
            # 台词原文（脚本里已无引号）用于从画面描述里剔除，避免字幕被画进图里
            "dialogue": source.get("text", "") if source.get("type") == "dialogue" else "",
            "start": float(row.get("start", 0)),
            "duration": float(row.get("duration", 0)),
            "visual_duration": float(row.get("visual_duration", row.get("duration", 0))),
            "title": segment_id == "title",
        })
    if not timeline or timeline[0]["segment_id"] != "title":
        raise VisualPlanError("音频时间轴必须以标题段开始")

    # The title/cover is always image one. Remaining rows become 5-9 semantic beats.
    body_rows = timeline[1:]
    total_duration = sum(row["visual_duration"] for row in body_rows)
    if max_images == 1:
        groups = [timeline]
    else:
        desired_body = min(max_images - 1, len(body_rows), max(1, round(total_duration / 11.0)))
        groups = [[timeline[0]]] + _partition(body_rows, desired_body)
    names = _character_names(facts)
    for name in (cast or {}):
        if name not in names and name != "旁白":
            names.append(name)

    scenes = []
    narrations = ["".join(row["text"] for row in group) for group in groups]
    # 场景判断要把**故事原文**一起给模型：只看单镜旁白看不出"中途回了家"这类转折
    # （原文里写着"把它带回家""第二天"，碎片里没有）。
    story_body = ""
    rel = str((script or {}).get("story_path") or "")
    if rel:
        try:
            story_body = (ROOT / rel).read_text(encoding="utf-8")
        except OSError:
            story_body = ""
    scene_plan: list[dict] = []
    scene_error = ""
    if llm_cfg:
        from . import audio_annotator
        try:
            scene_plan, scene_error = audio_annotator.plan_scenes(
                llm_cfg, narrations, story_body)
        except Exception as exc:          # 场景判断是增强，失败不能中断出片
            scene_error = str(exc)
        if scene_error:
            print(f"  [scene] 场景判断不可用（{scene_error}），回退到关键词规则")

    previous_scene = "家里客厅"
    for index, group in enumerate(groups, 1):
        text = narrations[index - 1]
        dialogue = tuple(row.get("dialogue") or "" for row in group)
        judged = scene_plan[index - 1] if index - 1 < len(scene_plan) else {}
        if judged.get("location"):
            scene = judged["location"]
            scene_base = judged.get("environment") or ""
        else:
            scene = _scene_name(text, previous_scene)
            scene_base = ""
        previous_scene = scene
        speakers = [row["speaker"] for row in group if row["speaker"] != "旁白"]
        present = [name for name in names if name in text or name in speakers]
        if index == 1 and not present:
            present = [name for name in ("牛牛", "添添", "爸爸", "妈妈") if name in names]
        character = speakers[0] if speakers else (present[0] if present else "旁白")
        start = group[0]["start"]
        end = group[-1]["start"] + group[-1]["visual_duration"]
        row = {
            "id": index,
            "start": round(start, 4),
            "end": round(end, 4),
            "duration": round(end - start, 4),
            "segment_ids": [row["segment_id"] for row in group],
            "speaker": character,
            "narration": text,
            "keywords": [],
            "title": index == 1,
            "scene": scene,
            "present": present,
            "character": character,
            "costume": "",
            "image_prompt": _prompt(scene, text, present, facts, dialogue, scene_base),
        }
        if scene_base:
            row["scene_base"] = scene_base
        scenes.append(row)

    plan = {
        "schema_version": 1,
        "kind": "visual_plan",
        "title": script.get("title", ""),
        "story_id": re.sub(r"[^\w-]", "", script.get("title", "")) or "audio_story",
        "audio_script_digest": digest,
        "audio_file": manifest.get("audio_file", ""),
        "image_count": len(scenes),
        "max_images": max_images,
        "audio_segments": timeline,
        "scenes": scenes,
    }
    validate_visual_plan(plan, manifest)
    return plan


def validate_visual_plan(plan: dict, manifest: dict) -> dict:
    errors = []
    scenes = plan.get("scenes") or []
    if plan.get("kind") != "visual_plan":
        errors.append("kind 必须是 visual_plan")
    if (len(scenes) > 10 or len(scenes) > int(plan.get("max_images", 10))
            or len(scenes) != plan.get("image_count")):
        errors.append("配图数量无效或超过 10 张")
    if plan.get("audio_script_digest") != manifest.get("audio_script_digest"):
        errors.append("配图方案与音频摘要不一致")
    expected = [str(row.get("segment_id") or "") for row in manifest.get("scenes") or []]
    covered = [str(seg) for scene in scenes for seg in scene.get("segment_ids") or []]
    if covered != expected:
        errors.append("音频段必须按原顺序且恰好被视觉节拍覆盖一次")
    previous_end = None
    for scene in scenes:
        start, end = float(scene.get("start", -1)), float(scene.get("end", -1))
        if start < 0 or end <= start:
            errors.append(f"视觉节拍 {scene.get('id')} 时间无效")
        if previous_end is not None and abs(start - previous_end) > 0.03:
            errors.append(f"视觉节拍 {scene.get('id')} 与前一节拍不连续")
        previous_end = end
    if scenes and not scenes[0].get("title"):
        errors.append("第一张图必须是标题/封面节拍")
    if errors:
        raise VisualPlanError("；".join(errors))
    return {"ok": True, "image_count": len(scenes), "covered_segments": len(covered)}

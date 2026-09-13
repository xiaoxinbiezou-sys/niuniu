"""Canonical audio-story script compiler and hard quality gate.

The video storyboard pipeline is intentionally not reused here. An audio script is
compiled deterministically from an approved story: prose is narration and only
quoted text can become character dialogue. Every segment keeps its source span,
so later edits cannot silently change facts, speakers, or wording.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .config import ROOT
from .story_policy import audio_policy


SCHEMA_VERSION = 1
QUOTE_CHARS = '"“”「」『』'
# Speech verbs that may introduce a quotation. 多字动词放前面，单字放最后，这样 _GUIDE_TAIL
# 里紧跟在动词后的量词不会有歧义，正则也不会从长选项回退
# （"说自己回答：" 不能被当成动词 "回答说"）。
SPEECH_VERBS = ("回答说|笑了起来|笑着说|笑着问|哭着说|大声说|小声说|喊道|叫道|吼道|问道|"
                "嘟囔|念叨|嘀咕|回答|笑起来|笑了|笑道|"
                # 母稿常用动作代替"说"来引导台词（"牛牛点点头：…"），这类也是引导语
                "点点头|摇摇头|拍拍手|拍拍胸脯|摸摸头|抬起头|低着头|想了想|叹口气|"
                "说|问|喊|叫|答|嚷|吼|笑|哭")

DEFAULT_DIALOGUE_EMOTION = "neutral"

# Narration guide → emotion. The guide clause before a quotation already states
# how the line is delivered ("不好意思地笑了", "拍着手喊"), so the emotion is read
# off the母稿 instead of being invented. Order inside a row does not matter;
# matching is longest-keyword-first, so "不好意思" beats "笑".
_EMOTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("awkward", ("不好意思", "尴尬", "挠挠头", "挠了挠头", "红着脸", "脸红", "羞", "挠头",
                 "支支吾吾", "吞吞吐吐", "结结巴巴", "讪讪")),
    ("cry", ("哭", "大哭", "哭了", "抽泣", "眼泪", "呜呜", "哇哇", "带着哭腔", "哭腔",
             "哽咽", "抹眼泪", "委屈")),
    ("anxious", ("急得", "着急", "焦急", "急坏", "急死", "等不及", "团团转", "坐立不安",
                 "急急忙忙")),
    ("scared", ("害怕", "吓", "发抖", "紧张", "缩成一团", "瑟瑟", "怯", "心惊", "恐怖")),
    ("angry", ("生气", "气鼓鼓", "气愤", "发火", "气呼呼", "恼火", "别闹", "不许")),
    ("excited", ("高兴", "开心", "兴奋", "欢呼", "拍手", "拍着手", "跳起来", "高兴极了",
                 "手舞足蹈", "笑成一团", "快乐", "太棒", "欢呼雀跃")),
    ("surprise", ("惊讶", "吃惊", "愣", "咦", "居然", "竟然")),
    ("mystery", ("神秘", "神神秘秘", "神秘兮兮", "故作神秘")),
    ("proud", ("骄傲", "得意", "神气", "炫耀", "自豪", "了不起", "显摆")),
    ("triumph", ("赢了", "赢啦", "成功", "成功啦", "学会", "学会啦", "第一名",
                 "终于", "原来", "哈哈")),
    ("laugh", ("咯咯", "嘻嘻", "嘿嘿", "哈哈", "噗嗤", "笑", "笑起来", "笑了")),
    ("tender", ("温柔", "轻轻", "轻柔", "轻声", "安慰", "心疼", "摸摸", "抱住", "贴心")),
    ("question", ("问", "好奇", "为什么", "怎么", "难道", "是不是", "什么呀", "问号")),
    ("whisper", ("小声", "悄悄", "低声", "耳语", "压低声音", "轻声说")),
)

_EMOTION_LOOKUP: tuple[tuple[str, str], ...] = tuple(
    sorted(((kw, emo) for emo, kws in _EMOTION_RULES for kw in kws),
           key=lambda item: (-len(item[0]), item[0]))
)

# Emotions the downstream engines can actually render.
ENGINE_EMOTIONS = frozenset({
    "narrate", "neutral", "awkward", "cry", "sad", "scared", "angry", "anxious",
    "excited", "surprise", "mystery", "proud", "triumph", "laugh", "tender",
    "question", "whisper", "happy", "brave", "gentle", "sleepy",
})


def _emotion_clauses(text: str) -> list[str]:
    """Split a guide segment into sentences, then into clauses (，、；).

    "……！爸爸挠挠头，不好意思地笑了：" → ["爸爸挠挠头", "不好意思地笑了："]
    """
    clauses: list[str] = []
    for sentence in re.split(r"[。！？!?\n]", text or ""):
        clauses.extend(part for part in re.split(r"[，,；;、]", sentence) if part.strip())
    return clauses


def _match_emotion(text: str) -> str:
    for keyword, emotion in _EMOTION_LOOKUP:
        if keyword in text:
            return emotion
    return DEFAULT_DIALOGUE_EMOTION


def _has_emotion_cue(text: str) -> bool:
    return any(keyword in text for keyword, _ in _EMOTION_LOOKUP)


def guess_dialogue_emotion(guide: str, quote: str = "") -> str:
    """Read the emotion of a quoted line off its narration guide (plus the line itself).

    ``guide`` is the **whole** narration segment that introduces the quotation — never a
    fixed-width slice of the母稿, because the sentence carrying the cue ("牛牛瞪大眼睛，
    然后咯咯咯地笑起来：") is often longer than any arbitrary cut and would silently drop.

    The cue is not always in the last clause: a segment may open with "牛牛高兴极了。" and
    then spend three clauses on action before "……嘴里念叨：", so scanning backwards and
    stopping at the first hit grabs whatever happens to sit last. Instead every clause
    carrying a cue is collected and the one **nearest the quotation** wins, which is the
    clause attached to the speech verb ("急得……嘴里念叨：" beats "高兴极了。"). The quoted
    line's own wording is consulted only when the whole guide is emotionless, so the result
    follows the母稿 rather than inventing emotion.
    """
    clauses = [c for c in _emotion_clauses(guide) if c.strip()]
    for clause in reversed(clauses):
        if _has_emotion_cue(clause):
            return _match_emotion(clause)
    if _has_emotion_cue(guide):
        return _match_emotion(guide)
    return _match_emotion(quote)


class AudioScriptError(RuntimeError):
    pass


# 质量门错误分类。批量流程据此决定"重写故事再试"还是"直接判死"。
# 判断集中在这里（而不是散在调用方的字符串匹配里），并有单元测试兜底。
RETRYABLE_CATEGORIES = ("speaker", "ratio", "streak", "coverage", "policy")
FATAL_CATEGORIES = ("source", "structure", "facts", "approval")

_SPEAKER_MARKS = ("speaker 无法确定", "旁白没有动作词", "没有旁白引导",
                  "没有正确引导说话人", "母稿语法指向", "台词段混入了说话人引导")
_RATIO_MARKS = ("旁白占比过低", "旁白占比偏高")
_STREAK_MARKS = ("连续人物台词过多",)
_COVERAGE_MARKS = ("文本与母稿来源不一致", "source 区间", "未完整覆盖母稿", "文本为空",
                   "台词较长")
_POLICY_MARKS = ("政策已更新",)
_SOURCE_MARKS = ("母稿哈希不一致", "无法读取母稿")
_FACTS_MARKS = ("事实表哈希不一致", "事实冲突", "关键事实缺失", "不在人物事实表中")
_APPROVAL_MARKS = ("尚未人工批准", "批准后脚本发生变化")
_STRUCTURE_MARKS = ("schema_version", "kind 必须是", "segments 不能为空",
                    "segment_id 缺失或重复", "type 必须是", "source 区间无效",
                    "区间乱序或重叠", "narrator 段必须")


def classify_errors(errors: list[str]) -> set[str]:
    """把质量门的错误串归到类别上。认不出来的归为 'unknown'（按不可重试处理）。"""
    categories: set[str] = set()
    for err in errors or []:
        text = str(err)
        matched = False
        for category, marks in (
            ("speaker", _SPEAKER_MARKS), ("ratio", _RATIO_MARKS),
            ("streak", _STREAK_MARKS), ("coverage", _COVERAGE_MARKS),
            ("policy", _POLICY_MARKS), ("source", _SOURCE_MARKS),
            ("facts", _FACTS_MARKS), ("approval", _APPROVAL_MARKS),
            ("structure", _STRUCTURE_MARKS),
        ):
            if any(mark in text for mark in marks):
                categories.add(category)
                matched = True
                break
        if not matched:
            categories.add("unknown")
    return categories


def is_retryable(errors: list[str]) -> bool:
    """母稿改写能否解决这些问题？只有改写能救的才重试。"""
    categories = classify_errors(errors)
    return bool(categories) and categories <= set(RETRYABLE_CATEGORIES)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(rf"[\s{re.escape(QUOTE_CHARS)}]", "", text)


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_path(path: str | Path, base: Path = ROOT) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base / p


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def read_story(path: str | Path) -> tuple[str, str, Path]:
    story_path = _resolve_path(path)
    raw = story_path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    title = story_path.stem
    body_start_line = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            title = line.strip().lstrip("#").strip() or title
            body_start_line = i + 1
            break
    body = "\n".join(lines[body_start_line:]).strip()
    if not body:
        raise AudioScriptError(f"故事正文为空: {story_path}")
    return title, body, story_path


def load_facts(path: str | Path | None) -> tuple[dict, Path | None]:
    if not path:
        return {}, None
    facts_path = _resolve_path(path)
    return _read_json(facts_path), facts_path


def validate_fact_text(text: str, facts: dict) -> list[str]:
    """Validate story-world facts against plain story or script text."""
    errors: list[str] = []
    for rule in facts.get("forbidden_patterns", []):
        pattern = rule.get("pattern", "") if isinstance(rule, dict) else str(rule)
        message = rule.get("message", pattern) if isinstance(rule, dict) else pattern
        if pattern and re.search(pattern, text):
            errors.append(f"事实冲突：{message}")
    for rule in facts.get("required_patterns", []):
        pattern = rule.get("pattern", "") if isinstance(rule, dict) else str(rule)
        message = rule.get("message", pattern) if isinstance(rule, dict) else pattern
        if pattern and not re.search(pattern, text):
            errors.append(f"关键事实缺失：{message}")
    return errors


def _character_aliases(facts: dict) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for row in facts.get("characters", []):
        if isinstance(row, str):
            name, names = row, [row]
        else:
            name = str(row.get("name", "")).strip()
            names = [name, *(row.get("aliases") or [])]
        if not name:
            continue
        for alias in names:
            if alias:
                aliases[str(alias)] = name
    return aliases


def _guide_segment(segments: list[dict], index: int, max_parts: int = 3) -> tuple[str, int]:
    """Narration that introduces the dialogue at ``index``, plus how many segments it spans.

    ``index`` is the dialogue's position. The narration directly above it is used, and the
    guide can span several narrator segments: the narration length cap may cut a sentence in
    two — "……解开缠住的水草，小蓝鱼" / "地游出来，吐了一串亮晶晶的泡泡：" — which would
    otherwise separate the speaker's name from its speech verb. Contiguous narrator segments
    are stitched back together until a sentence boundary is reached, so the speaker decision
    always sees the whole sentence. Compiler and validator both call this, so the decision
    cannot drift between them.
    """
    parts: list[str] = []
    position = index - 1
    while 0 <= position < len(segments) and len(parts) < max_parts:
        segment = segments[position]
        if segment.get("type") != "narrator":
            break
        parts.insert(0, str(segment.get("text", "")))
        # parts[0] is the earliest segment collected. When an earlier sentence terminator
        # sits inside it, the sentence carrying the speech verb has been fully recovered,
        # and anything above is a different scene.
        if re.search(r"[。！？!?]", parts[0][:-1]):
            break
        position -= 1
    return "".join(parts), len(parts)


def _following_narration(segments: list[dict], index: int, max_parts: int = 2) -> str:
    """台词**之后**紧邻的旁白（用于后置引导语 '"……"爸爸问。'）。

    ``index`` 是台词自身的位置。与 _guide_segment 的方向相反，这个函数往后看。
    """
    parts: list[str] = []
    position = index + 1
    while position < len(segments) and len(parts) < max_parts:
        segment = segments[position]
        if segment.get("type") != "narrator":
            break
        parts.append(str(segment.get("text", "")))
        if re.search(r"[。！？!?]", "".join(parts)[:-1]):
            break
        position += 1
    return "".join(parts)


def _has_speech_verb(guide: str) -> bool:
    """True when the guide's final clause ends in a speech verb (optionally + colon)."""
    clause = re.split(r"[。！？!?\n]", guide or "")[-1]
    return bool(re.search(rf"(?:{SPEECH_VERBS})\s*[：:]?\s*$", clause))


# A guide may put the listener between the verb and the quotation, plus a manner particle:
# "添添赶紧问爸爸：“…”" or "小蓝鱼地说：“…”". The gap is deliberately short (≤4) so an
# action clause cannot masquerade as a guide ("小蓝鱼地游出来，吐了泡泡：" must not match).
# The lookbehind keeps "自己回答：" inside the 自己 branch instead of matching "回答" here.
_GUIDE_TAIL = (rf"(?<!自己)(?:{SPEECH_VERBS})[^。！？!?\n：:]{{0,4}}[：:]?\s*$")

# 母稿不必把引导语放在台词前。"……"爸爸问。是规范的中文写法，模型也常这么写：
# 只有前置引导会被 _GUIDE_TAIL 认出来，于是整句台词变成"认不出说话人"而卡住流程。
# 后置引导 = 名字 + 言语动词，出现在引号之后、句号之前。
_TRAILING_GUIDE = (rf"^[\s，,、]*(?P<name>[\u4e00-\u9fa5]{{2,4}})"
                   rf"[^。！？!?\n]{{0,6}}?(?:{SPEECH_VERBS})")


# Words that a naive "name immediately before a speech verb" scan would otherwise turn
# into a speaker ("吐了一串亮晶晶的泡泡：" → not a character).
_NOT_A_NAME = frozenset({
    "泡泡", "水草", "钥匙", "声音", "时候", "地方", "东西", "样子", "意思", "故事",
    "大家", "一起", "然后", "突然", "忽然", "终于", "于是", "接着", "一天",
    "嘴里", "心里", "手里", "眼里", "脸上", "身后", "怀里", "头上",
    "挠挠头", "点点头", "摇摇头", "皱皱眉", "眨眨眼", "拍拍手", "低下头", "抬起头",
})


def _name_before_speech_verb(story_body: str, start, end, aliases: dict[str, str]) -> str:
    """Character name the母稿 uses to introduce this line, when facts does not define it.

    Used only to make the quality-gate error actionable ("母稿里看起来是「萤火虫」，请把它
    加入事实表") — never to hand the line a voice. The name is read as the subject position
    right before the speech verb, so it is reported only when the母稿 really names someone.
    """
    if not isinstance(start, int):
        return ""
    window = story_body[max(0, start - 40):max(0, start - 1)]
    clause = re.split(r"[。！？!?\n]", window)[-1]
    found = re.search(r"([\u4e00-\u9fa5]{2,4})(?:" + _GUIDE_TAIL + ")", clause)
    if not found:
        return ""
    name = found.group(1)
    if name in aliases or name in _NOT_A_NAME or name[0] in "他她它我你您":
        return ""
    if name.endswith(("地", "的", "然", "得")):
        return ""
    return name


# 后置引导语专用动词：只有明确表示"在说话"的才算。
# "爸爸笑了。""妈妈听见以后笑了。" 这类是动作描写，不是对台词的引导——
# 收进来会压掉正确的前置引导（"添添赶紧问爸爸："）。
_TRAILING_VERBS = ("回答说|笑着说|笑着问|哭着说|大声说|小声说|问道|喊道|叫道|吼道|回答|"
                   "嘟囔|念叨|嘀咕|说|问|喊|叫|答|嚷|吼")

def _post_guide(text: str, aliases: dict[str, str]) -> str:
    """台词之后的后置引导语，例如 '"虫子在哪儿？"爸爸问。' 里的 '爸爸问。'。

    必须同时满足三条，缺一不可：
      1. 紧跟收尾引号之后，且不再出现引号（否则那是"下一句台词的引导语"）；
      2. **以句号/叹号/问号收尾**。冒号结尾的是引号下一句台词的**前置**引导
         （'"……笑起来："下一句"'），不能拿来给这一句定说话人；
      3. 不再出现引号（否则那是"下一句台词的引导语"）。
    """
    tail = (text or "").lstrip(QUOTE_CHARS + " ")
    if not tail:
        return ""
    verb = re.compile(rf"(?:{_TRAILING_VERBS})")
    # 逐个句子试：真正的后置引导是"爸爸问。""爸爸站在椅子上，嘴硬地说。"
    # 这类**子句开头是名字、子句里有言语动词、并以句号/叹号/问号收尾**的短句。
    for found in re.finditer(r"[^。！？!?\n]{1,40}[。！？!?]", tail):
        clause = found.group(0)
        if "：" in clause or ":" in clause:
            continue
        if any(ch in clause for ch in QUOTE_CHARS):
            continue
        if not verb.search(clause):
            continue
        head = clause.lstrip("，,、 ")
        for alias in sorted(aliases, key=len, reverse=True):
            if not head.startswith(alias):
                continue
            # 名字后面必须**紧跟**言语动词（"爸爸问。""爸爸说。"），或者只隔一个逗号
            # （"爸爸站在椅子上，嘴硬地说。"）。放宽成"名字…动词"会把
            # "妈妈听见以后笑了。" 这种普通旁白也当成引导，压掉正确的前置引导。
            rest = head[len(alias):]
            # 名字后可以隔一小段（"爸爸站在椅子上，嘴硬地说。"），但中间不能跨句号，
            # 而且必须是 _TRAILING_VERBS 里"明确表示在说话"的动词。
            if re.match(rf"[^。！？!?\n]{{0,14}}?(?:{_TRAILING_VERBS})", rest):
                return clause.strip()
    return ""


def _trailing_guide_speaker(trailing: str, aliases: dict[str, str]) -> str:
    """从后置引导语里读说话人：'爸爸问。' → 爸爸。

    入参必须是 _post_guide 已经认可的短句。不能收普通旁白——
    "妈妈听见以后笑了。" 里"笑"也在动词表里，宽着收会把前置引导压掉。
    """
    clause = (trailing or "").strip().rstrip("。！？!?")
    if not clause:
        return ""
    head = clause.lstrip("，,、 “”\"'")
    for alias in sorted(aliases, key=len, reverse=True):
        if head.startswith(alias):
            return aliases[alias]
    return ""


def _resolve_speaker(context: str, aliases: dict[str, str], last_speaker: str,
                     trailing: str = "") -> str:
    full = re.split(r"[。！？!?\n]", context)[-1]
    clause = full[-80:]
    ordered_aliases = sorted(aliases, key=len, reverse=True)

    # '"……"爸爸问。' —— 后置引导语是母稿对这句台词最直接的说明，优先采信。
    from_trailing = _trailing_guide_speaker(trailing, aliases)
    if from_trailing:
        return from_trailing

    # Without a speech verb the narration only describes an action ("他往螃蟹壳上一敲，"),
    # so it cannot attribute the following quotation to anyone.
    if not re.search(rf"(?:{SPEECH_VERBS})", clause):
        return ""

    # In ordinary Chinese narration the character at the start of the clause is
    # the grammatical subject; later names are often objects (for example
    # "牛牛钻进妈妈怀里，认真地说").
    if re.search(_GUIDE_TAIL, clause):
        stripped = clause.lstrip("，,；;：: ")
        for alias in ordered_aliases:
            if stripped.startswith(alias):
                return aliases[alias]

    # "牛牛不等小人回答，自己又说": 自己 refers to the clause subject,
    # not the character nearest to the speech verb.
    if re.search(rf"自己(?:又|赶紧|小声|大声)?(?:地)?(?:{_GUIDE_TAIL})", clause):
        for alias in ordered_aliases:
            if clause.startswith(alias):
                return aliases[alias]

    candidates: list[tuple[int, str]] = []
    # A name is credited only in its own sub-clause: at most one comma may separate it from
    # the speech verb, and the verb must follow that comma directly. So
    # "牛牛急得围着铁罐转了八圈，嘴里念叨：" resolves to 牛牛 (the comma leads straight into
    # the verb), while "小蓝鱼地游出来，吐了一串泡泡：" does not resolve at all because the
    # second sub-clause holds no speech verb. The lookbehind stops an alias matching inside
    # a longer word.
    for alias in ordered_aliases:
        pattern = (rf"(?<![\u4e00-\u9fa5]){re.escape(alias)}[^。！？!?\n]{{0,16}}?"
                   rf"(?:[，,])?(?:{_GUIDE_TAIL})")
        for match in re.finditer(pattern, clause):
            candidates.append((match.start(), aliases[alias]))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    # 长引导语（超过 80 字）会让 clause 被从中间截断，句首主语就丢了：
    # "……什么都没有！爸爸挠挠头，不好意思地笑了：" 截断后开头是"摇摇头，…"。
    # 这种时候回到未截断的句子，看第一个逗号之前有没有主语。
    if full != clause:
        first_part = full.split("，")[0].lstrip("，,；;：: ")
        for alias in ordered_aliases:
            if first_part.startswith(alias):
                return aliases[alias]

    # The subject can precede its own clause: "……水草，小蓝鱼地游出来，吐了一串亮晶晶的
    # 泡泡：" — the only clue is that the first名字 in the clause is its subject. A later
    # name after a comma is still treated as an object and deliberately not guessed.
    first_clause = clause.split("，")[0].lstrip("，,；;：: ")
    for alias in ordered_aliases:
        if first_clause.startswith(alias):
            return aliases[alias]

    # A pronoun-led line ("他说：…" / "他奇怪地问：…") is only safe when the guide verb
    # ends the clause. Otherwise the nearest名前 candidate is merely an object
    # ("妈妈对牛牛说"), and guessing would hand the line to the wrong voice; return ""
    # so the caller fails the quality gate instead of rendering a wrong speaker.
    if re.search(rf"(?:他|她|自己)[^。！？!?\n：:]{{0,6}}(?:{_GUIDE_TAIL})", clause):
        return last_speaker
    return ""


def _segment_id(kind: str, start: int, end: int, source_digest: str) -> str:
    seed = f"{source_digest}:{kind}:{start}:{end}"
    return f"seg_{start:05d}_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:8]}"


def _append_segment(segments: list[dict], kind: str, speaker: str, text: str,
                    start: int, end: int, source_digest: str,
                    emotion: str = "") -> None:
    cleaned = re.sub(r"\s+", "", text).strip()
    if not cleaned:
        return
    if not emotion:
        emotion = "narrate" if kind == "narrator" else DEFAULT_DIALOGUE_EMOTION
    segments.append({
        "segment_id": _segment_id(kind, start, end, source_digest),
        "type": kind,
        "speaker": speaker,
        "text": cleaned,
        "emotion": emotion,
        "source": {"start": start, "end": end},
    })


def _narration_blocks(body: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """把 [start, end) 的旁白切成若干 (起, 止) 区间。

    **只在句末标点处断开，绝不在句子中间硬切。**

    以前为了凑 max_chars，会在任意字符位置切断。切点一旦落在句子中间就会出事：
    "爸爸'哇'地跳上沙发，大叫：" 被切成 "……跳上沙发" + "地跳上沙发，大叫："，
    听众先听到半句、再听到带"地"的重复——实测《什么都不怕的爸爸》里有 3 处。
    宁可让单段超出 max_chars（TTS 能念长句），也不能把句子拦腰截断。
    """
    blocks: list[tuple[int, int]] = []
    chunk_start = start
    visible = 0
    for i in range(start, end):
        ch = body[i]
        if not ch.isspace():
            visible += 1
        if ch in "。！？!?；;\n" and visible >= max_chars // 2:
            blocks.append((chunk_start, i + 1))
            chunk_start = i + 1
            visible = 0
    if chunk_start < end:
        blocks.append((chunk_start, end))
    return blocks


def _append_narration(segments: list[dict], body: str, start: int, end: int,
                      source_digest: str, max_chars: int) -> None:
    """输出 [start, end) 的旁白。

    边界上的引号字符直接吃掉：调用方传进来的 end 常常正好停在开引号之前
    （"……张开翅膀，" 后面紧跟开引号），如果把这个引号当成旁白，旁白就以逗号结尾、
    下一段从引号后面的"地飞了起来"开始，听起来就是半句话加重复（实测 3 处）。
    引号只属于台词，吃掉它，旁白就停在完整的句子成分上。
    """
    while start < end and body[start] in QUOTE_CHARS:
        start += 1
    while end > start and body[end - 1] in QUOTE_CHARS:
        end -= 1
    for chunk_start, chunk_end in _narration_blocks(body, start, end, max_chars):
        _append_segment(segments, "narrator", "旁白", body[chunk_start:chunk_end],
                        chunk_start, chunk_end, source_digest)


def _quote_matches(body: str):
    pattern = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|「([^」\n]+)」|『([^』\n]+)』')
    return pattern.finditer(body)


def _emit_dialogue(segments: list[dict], body: str, q_start: int, q_end: int, quote_text: str,
                   decision: dict | None, aliases: dict[str, str], source_digest: str,
                   last_speaker: str) -> int:
    """输出一段台词，返回新的游标位置（引号结尾）。

    说话人优先取 AI 标注；标注缺失时回退到语法推断。

    回退也定不出说话人的**极短引语**（单字语气词 "哇""嗡""啪" 这类）不当独立台词，
    而是并进旁白。否则会出现两个问题：
      1. 它后面那个结构助词（"哇"**地**跳上沙发）会孤立成旁白"地跳上沙发"，
         上一段又是半句"……跳上沙发"，听起来就是重复；
      2. 没有说话人 → 配音不知道该用谁的声音 → 质量门拦下整条流程。
    真实流程里这些语气词本来就该由标注层转成旁白（实测标注正是这么处理的），
    这里只是把回退路径也对齐。
    """
    speaker = ""
    if isinstance(decision, dict):
        speaker = str(decision.get("speaker") or "").strip()
    if not speaker:
        narration_before, _ = _guide_segment(segments, len(segments))
        trailing = _post_guide(body[q_end:q_end + 30], aliases)
        speaker = _resolve_speaker(
            narration_before or body[max(0, q_start - 80):q_start],
            aliases, last_speaker, trailing=trailing)
    narration_before, _ = _guide_segment(segments, len(segments))
    trailing = _post_guide(body[q_end:q_end + 30], aliases)
    emotion = guess_dialogue_emotion(narration_before or trailing, quote_text)
    _append_segment(segments, "dialogue", speaker, quote_text,
                    q_start + 1, q_end - 1, source_digest, emotion=emotion)
    return q_end


def _strip_quotes(text: str) -> str:
    """去掉文本里的引号字符（引号只用于标记台词，不参与朗读）。"""
    return re.sub(rf"[{re.escape(QUOTE_CHARS)}]", "", text or "")


def _monotonic(segments: list[dict], body: str) -> list[dict]:
    """把段落整理成"来源区间严格递增、无空段"。

    合并语气词时会把相邻几段并成一段，合并后的区间可能与前一段重叠；
    校验器是按 source 区间去母稿取原文来核对的，所以这里必须收干净：
    区间被夹逼后按新区间重算文本，空段直接丢弃。
    """
    out: list[dict] = []
    cursor = 0
    for seg in sorted(segments, key=lambda s: int(s["source"]["start"])):
        start = max(int(seg["source"]["start"]), cursor)
        end = int(seg["source"]["end"])
        if end <= start:
            continue
        seg["source"] = {"start": start, "end": end}
        seg["text"] = _strip_quotes(body[start:end])
        out.append(seg)
        cursor = end
    return out


def split_sentences(body: str) -> list[tuple[int, int, str]]:
    """把母稿切成**小块**，返回 [(起, 止, 原文)]，供改编层引用编号。

    切法只有两条机械规则，不做任何"语义判断"：
      1. 一段引号（含引号本身）单独成块；
      2. 引号之外的句末标点（。！？；换行）处断开。

    所以一个 60 字的长句会被切成若干短块，模型可以自由地把它们**成组**，
    "多大一段合适"由模型决定；而正文永远由程序按块区间取出，
    **重复或漏字在结构上不可能发生**。
    """
    blocks: list[tuple[int, int, str]] = []
    start = 0
    i = 0

    def _with_trailing_space(end: int) -> int:
        # 块带上自己的尾部空白（段落之间的空行），这样各块拼起来与母稿逐字相同
        while end < len(body) and body[end].isspace():
            end += 1
        return end

    while i < len(body):
        ch = body[i]
        if ch in QUOTE_CHARS:
            if i > start:
                blocks.append((start, i, body[start:i]))
            end = i + 1
            while end < len(body) and body[end] not in QUOTE_CHARS and body[end] != "\n":
                end += 1
            if end < len(body) and body[end] in QUOTE_CHARS:
                end += 1
            end = _with_trailing_space(end)
            blocks.append((i, end, body[i:end]))
            start = i = end
            continue
        if ch in "。！？!?；;\n":
            end = _with_trailing_space(i + 1)
            blocks.append((start, end, body[start:end]))
            start = i = end
            continue
        i += 1
    if start < len(body):
        blocks.append((start, len(body), body[start:]))
    return [b for b in blocks if b[2].strip()]


def _compile_audio_script_data(title: str, body: str, facts: dict,
                               story_path: str, facts_path: str = "") -> dict:
    """规则断句的确定性编译（AI 改编不可用时的回退路径）。

    它按"引号即台词"机械切分，并用语法推断说话人。因为切分是机械的，遇到
    "……爸爸“哇”地跳上沙发" 这类写法会切出半句话；正常流程走
    compile_with_annotation（AI 断句），这里只在改编失败时兜底。
    """
    aliases = _character_aliases(facts)
    if not aliases:
        raise AudioScriptError("事实表必须定义 characters，才能确定台词 speaker")

    source_digest = _digest(body)
    segments: list[dict] = []
    cursor = 0
    last_speaker = ""
    max_chars = int((facts.get("policy") or {}).get("narrator_segment_max_chars", 90))

    for match in _quote_matches(body):
        q_start, q_end = match.start(), match.end()
        _append_narration(segments, body, cursor, q_start, source_digest, max_chars)
        quote_text = next(group for group in match.groups() if group is not None)
        cursor = _emit_dialogue(segments, body, q_start, q_end, quote_text, None,
                                aliases, source_digest, last_speaker)
        if segments and segments[-1]["type"] == "dialogue" and segments[-1]["speaker"]:
            last_speaker = segments[-1]["speaker"]
    _append_narration(segments, body, cursor, len(body), source_digest, max_chars)

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_story",
        "status": "draft",
        "title": title,
        "story_path": story_path,
        "story_digest": source_digest,
        "facts_path": facts_path,
        "facts_digest": _digest(json.dumps(facts, ensure_ascii=False, sort_keys=True)) if facts else "",
        "policy": audio_policy(str(facts.get("story_type") or "family")),
        "segments": segments,
        "approval": None,
    }


def compile_audio_script(story_path: str | Path, facts_path: str | Path | None = None) -> dict:
    title, body, resolved_story = read_story(story_path)
    facts, resolved_facts = load_facts(facts_path)
    return _compile_audio_script_data(
        title, body, facts, _relative(resolved_story),
        _relative(resolved_facts) if resolved_facts else "",
    )


def compile_audio_script_text(title: str, body: str, facts: dict,
                              story_path: str, facts_path: str = "") -> dict:
    """Compile Studio-managed text while retaining canonical source paths."""
    if not body.strip():
        raise AudioScriptError("故事正文为空")
    return _compile_audio_script_data(title, body, facts, story_path, facts_path)


def _absorb_sandwiched_interjections(segments: list[dict], body: str) -> None:
    """把夹在旁白中间的**极短语气词**并回旁白。

    原文 "爸爸“哇”地跳上沙发，大叫：" 会被切成三段：
        '爸爸' / '“哇”' / '地跳上沙发，大叫：'
    "哇"只有一声，不该单独占一段配音（要单独合成一次音频、听感也碎）；
    把它并回旁白后，这一段完整念成"爸爸哇地跳上沙发，大叫："，标点不再出声。

    只处理**被旁白夹住、且不超过 3 个字**的台词段，正常台词不受影响。
    """
    merged = True
    while merged:
        merged = False
        for i, seg in enumerate(segments):
            if seg.get("type") != "dialogue":
                continue
            if len(_normalize(seg.get("text", ""))) > 3:
                continue
            if i == 0 or i + 1 >= len(segments):
                continue
            prev, nxt = segments[i - 1], segments[i + 1]
            if prev.get("type") != "narrator" or nxt.get("type") != "narrator":
                continue
            start = int(prev["source"]["start"])
            end = int(nxt["source"]["end"])
            merged = [{
                "segment_id": _segment_id("narrator", start, end, _digest(body)),
                "type": "narrator",
                "speaker": "旁白",
                "text": _strip_quotes(body[start:end]),
                "emotion": "narrate",
                "source": {"start": start, "end": end},
            }]
            # 合并会把前后段的来源区间吞进来，可能和前一段重叠；
            # 重排一次，保证区间严格递增（校验按区间取原文核对）。
            segments[i - 1: i + 2] = _monotonic(segments[:i - 1] + merged + segments[i + 2:], body)
            merged = True
            break


def _compile_from_segments(title: str, body: str, facts: dict, story_path: str,
                           facts_path: str, decisions: dict[int, dict],
                           meta: dict | None = None,
                           indexed: list[tuple[int, int, str]] | None = None) -> dict:
    """按模型对每个片段的判断装配脚本。

    模型只回答"这一段谁念"，正文由程序按片段区间取出，**重复或漏字在结构上不可能发生**。
    连续的旁白片段合并成一段（避免碎成一句一段）；台词片段单独成段。
    模型漏判的片段按旁白处理。
    """
    blocks = indexed if indexed is not None else split_sentences(body)
    source_digest = _digest(body)
    segments: list[dict] = []

    pending: list[int] = []          # 待合并的连续旁白片段

    def flush_narration() -> None:
        if not pending:
            return
        start = blocks[pending[0]][0]
        end = blocks[pending[-1]][1]
        _append_segment(segments, "narrator", "旁白", _strip_quotes(body[start:end]),
                        start, end, source_digest)
        pending.clear()

    def emotion_for(decision: dict, index: int) -> str:
        """台词情绪：优先用改编层给的情绪，没有才退回按引导语关键词推断。

        改编层（AI）正在读整篇，上下文比关键词全得多；关键词推断只是兜底，
        而且它依赖"前置引导语"里的神态词，标注模式下经常一个都拿不到。
        """
        chosen = str(decision.get("emotion") or "").strip().lower()
        if chosen and chosen != DEFAULT_DIALOGUE_EMOTION:
            return chosen
        # 兜底：用前后旁白块拼出上下文再推断
        context = ""
        for j in range(max(0, index - 2), index):
            if decisions.get(j, {}).get("type") != "dialogue":
                context += blocks[j][2]
        for j in range(index + 1, min(len(blocks), index + 3)):
            if decisions.get(j, {}).get("type") != "dialogue":
                context += blocks[j][2]
        return guess_dialogue_emotion(context, blocks[index][2])

    for i, (start, end, _text) in enumerate(blocks):
        decision = decisions.get(i) or {"type": "narrator"}
        if decision.get("type") == "dialogue":
            flush_narration()
            # 引号只用于标记台词，不参与朗读，所以两类段落都要去掉引号。
            # 区间保留**完整块区间**：早期版本把区间也往内缩一格去躲引号，
            # 结果文本里剩一个孤零零的闭引号，配音会把它念出来。
            _append_segment(segments, "dialogue", decision.get("speaker", ""),
                            _strip_quotes(body[start:end]), start, end,
                            source_digest, emotion=emotion_for(decision, i))
        else:
            pending.append(i)
    flush_narration()
    _absorb_sandwiched_interjections(segments, body)
    segments[:] = _monotonic(segments, body)

    policy = audio_policy(str(facts.get("story_type") or "family"))
    script = {
        "schema_version": SCHEMA_VERSION,
        "kind": "audio_story",
        "status": "draft",
        "title": title,
        "story_path": story_path,
        "story_digest": source_digest,
        "facts_path": facts_path,
        "facts_digest": _digest(json.dumps(facts, ensure_ascii=False, sort_keys=True)) if facts else "",
        "policy": policy,
        "segments": segments,
        "approval": None,
    }
    if meta:
        script["adaptation"] = meta
    return script


def compile_with_annotation(title: str, body: str, facts: dict,
                            story_path: str = "", facts_path: str = "",
                            llm_cfg: dict | None = None,
                            adapt: bool = True) -> dict:
    """带 AI 改编的编译入口。

    ``adapt=True`` 时先把母稿切成句子，让模型决定"哪些句子成一段、这段是旁白还是
    谁说的"（模型只回编号，不重打正文），程序按编号取原文装配。
    改编不可用时**自动回退**到规则编译，保证流程不会因为改编失败而中断。
    """
    from . import audio_annotator

    meta: dict = {"provider": "", "model": "", "error": ""}
    if llm_cfg and adapt:
        aliases = _character_aliases(facts)
        blocks = split_sentences(body)
        plan, error = audio_annotator.plan_script(
            llm_cfg, [b[2] for b in blocks],
            sorted(aliases, key=len, reverse=True))
        meta = {"provider": llm_cfg.get("provider", ""), "model": llm_cfg.get("model", ""),
                "error": error, "blocks": len(blocks), "decided": len(plan)}
        if not error:
            return _compile_from_segments(title, body, facts, story_path,
                                          facts_path, plan, meta, indexed=blocks)
        print(f"  [annotator] 改编不可用（{meta['error']}），回退到规则断句")
    script = _compile_audio_script_data(title, body, facts, story_path, facts_path)
    if meta.get("error"):
        script["adaptation"] = meta      # 记下为什么回退，便于排查
    return script


def _approval_payload(script: dict) -> dict:
    return {
        "schema_version": script.get("schema_version"),
        "kind": script.get("kind"),
        "title": script.get("title"),
        "story_path": script.get("story_path"),
        "story_digest": script.get("story_digest"),
        "facts_path": script.get("facts_path"),
        "facts_digest": script.get("facts_digest"),
        "policy": script.get("policy"),
        "segments": script.get("segments"),
    }


def approval_digest(script: dict) -> str:
    payload = json.dumps(_approval_payload(script), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return _digest(payload)


def _source_for_script(script: dict) -> tuple[str, dict]:
    _, body, _ = read_story(script.get("story_path", ""))
    facts, _ = load_facts(script.get("facts_path") or None)
    return body, facts


def validate_audio_script(script: dict, story_body: str | None = None,
                          facts: dict | None = None, require_approved: bool = False) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    # 脚本层改写过哪些对白（对白→旁白）。改写段落文本与母稿不同是允许的，
    # 但必须登记在案，并且逐条通过下面"不许夹带引号、不许扩写"的校验。
    rewrites = list((script.get("annotation") or {}).get("rewrites") or [])
    if script.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"不支持的 schema_version: {script.get('schema_version')}")
    if script.get("kind") != "audio_story":
        errors.append("kind 必须是 audio_story")

    if story_body is None or facts is None:
        try:
            loaded_body, loaded_facts = _source_for_script(script)
            story_body = story_body if story_body is not None else loaded_body
            facts = facts if facts is not None else loaded_facts
        except Exception as exc:
            errors.append(f"无法读取母稿或事实表: {exc}")
            story_body = story_body or ""
            facts = facts or {}

    if script.get("story_digest") != _digest(story_body):
        errors.append("母稿哈希不一致：故事已修改，必须重新编译音频脚本")
    current_facts_digest = _digest(json.dumps(facts, ensure_ascii=False, sort_keys=True)) if facts else ""
    if script.get("facts_digest", "") != current_facts_digest:
        errors.append("事实表哈希不一致：尺度/人物规则已修改，必须重新编译")

    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("segments 不能为空")
        segments = []

    ids: set[str] = set()
    narrator_chars = 0
    dialogue_chars = 0
    consecutive_dialogue = 0
    max_streak = 0
    reconstructed: list[str] = []
    previous_end = 0
    last_dialogue_speaker = ""
    aliases = _character_aliases(facts)
    expected_policy = audio_policy(str(facts.get("story_type") or "family"))
    canonical_aliases: dict[str, set[str]] = {}
    for alias, canonical in aliases.items():
        canonical_aliases.setdefault(canonical, set()).add(alias)

    for index, segment in enumerate(segments, 1):
        seg_id = str(segment.get("segment_id", ""))
        kind = segment.get("type")
        speaker = str(segment.get("speaker", ""))
        text = str(segment.get("text", "")).strip()
        source = segment.get("source") or {}
        start, end = source.get("start"), source.get("end")
        if not seg_id or seg_id in ids:
            errors.append(f"第 {index} 段 segment_id 缺失或重复")
        ids.add(seg_id)
        if kind not in ("narrator", "dialogue"):
            errors.append(f"{seg_id}: type 必须是 narrator/dialogue")
        if not text:
            errors.append(f"{seg_id}: text 为空")
        if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(story_body)):
            errors.append(f"{seg_id}: source 区间无效")
            source_text = ""
        else:
            if start < previous_end:
                errors.append(f"{seg_id}: source 区间乱序或重叠")
            previous_end = end
            source_text = story_body[start:end]
            if _normalize(source_text) != _normalize(text):
                # 脚本层有权把对白改写成旁白（见 audio_annotator）。这类段落文本必然
                # 与母稿不同，所以只对**登记在案**的改写放行，其余照旧一律拦截。
                rewritten = next((r for r in rewrites
                                  if tuple(r.get("span") or ()) == (start, end)), None)
                if rewritten is not None:
                    # 改写内容不许夹带引号（那等于把对白换个写法又塞回来）；
                    # 也不许离谱地膨胀——正常转述会补出说话人和一点动作，
                    # 但把它写成一段新情节就不对了。尺度定得很宽（2.5 倍且 40 字以上），
                    # 因为过紧的字符数规则会误杀正常转述（"啪"→"牛牛踮着脚走过去，
                    # 啪地一声把虫子扣住了"）；拦扩写主要靠"覆盖率"那条硬校验。
                    if any(ch in text for ch in QUOTE_CHARS):
                        errors.append(f"{seg_id}: 改写旁白里不能出现引号")
                    else:
                        base_len = len(_normalize(str(rewritten.get("quote") or "") or source_text))
                        limit = max(40, int(base_len * 2.5))
                        if len(_normalize(text)) > limit:
                            errors.append(
                                f"{seg_id}: 改写旁白比原台词长太多"
                                f"（{len(_normalize(text))}>{limit} 字），禁止扩写情节"
                            )
                        # 改成旁白后必须点明是谁在说，否则听众不知道这话是谁说的
                        names = set()
                        for src_name, variants in canonical_aliases.items():
                            names |= set(variants or ()) | {src_name}
                        if names and not any(name and name in text for name in names):
                            warnings.append(
                                f"{seg_id}: 改写后的旁白没有点出说话人，"
                                f"听众可能不知道这句是谁说的"
                            )
                else:
                    errors.append(f"{seg_id}: 文本与母稿来源不一致，禁止后期直接改段落")
        reconstructed.append(text)

        char_count = len(_normalize(text))
        if kind == "narrator":
            narrator_chars += char_count
            consecutive_dialogue = 0
            if speaker != "旁白":
                errors.append(f"{seg_id}: narrator 段必须使用旁白 speaker")
        elif kind == "dialogue":
            dialogue_chars += char_count
            consecutive_dialogue += 1
            max_streak = max(max_streak, consecutive_dialogue)
            if not speaker or speaker == "旁白":
                unresolved = _name_before_speech_verb(story_body, start, end, aliases)
                hint = (f"（母稿里看起来是「{unresolved}」，请把它加入事实表 characters "
                        f"或系列角色卡，或改成已有的角色名）") if unresolved else \
                       "（旁白里没有可识别的角色名，请用「角色名+说/问/喊」引导）"
                errors.append(f"{seg_id}: 台词 speaker 无法确定或不在人物事实表中{hint}")
            elif speaker not in canonical_aliases:
                errors.append(f"{seg_id}: 台词 speaker「{speaker}」不在人物事实表中"
                              f"（请把该角色加入 facts.characters）")
            else:
                last_dialogue_speaker = speaker
                # 说话人由 AI 标注决定（见 audio_annotator），所以"母稿语法必须能推出
                # 说话人"不再是正确性问题，只是听感问题：没有引导语，听众可能一时
                # 分不清谁在说。因此降为提示，不再拦流程。
                guide_text, _ = _guide_segment(segments, index - 1)
                trailing = _post_guide(story_body[end:end + 30], aliases) or \
                    _post_guide(_following_narration(segments, index - 1)[:30], aliases)
                if not guide_text and not trailing:
                    warnings.append(
                        f"{seg_id}: 这句台词前后没有旁白引导，听众可能分不清谁在说话"
                    )
                elif not (_has_speech_verb(guide_text) or _has_speech_verb(trailing)
                          or _trailing_guide_speaker(trailing, aliases)):
                    warnings.append(
                        f"{seg_id}: 引导语里没有「说/问/喊/叫」这类词，"
                        f"说话人靠标注确定，听感上可能不够清楚"
                    )
            if re.search(rf"(?:{SPEECH_VERBS})\s*[：:]", text):
                errors.append(f"{seg_id}: 台词段混入了说话人引导或旁白")
            max_dialogue = int(expected_policy["dialogue_max_chars"])
            if char_count > max_dialogue:
                warnings.append(
                    f"{seg_id}: 台词较长（{char_count}>{max_dialogue} 字），建议在自然停顿处分段"
                )

    # 覆盖率校验：脚本必须完整覆盖母稿，且不含母稿之外的内容。
    # 有改写时，把母稿里被改写的那几段换成改写后的句子，再做逐字比对——
    # 这样"改写"就被限制成"同一位置的替换"，不可能凭空插入或丢弃内容。
    expected = story_body
    for rw in sorted(rewrites, key=lambda r: tuple(r.get("span") or (0, 0)), reverse=True):
        span = tuple(rw.get("span") or ())
        if len(span) != 2:
            continue
        start, end = span
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(expected)):
            errors.append("改写记录区间无效")
            continue
        expected = expected[:start] + str(rw.get("text") or "") + expected[end:]
    source_without_quotes = re.sub(rf"[{re.escape(QUOTE_CHARS)}]", "", expected)
    if _normalize("".join(reconstructed)) != _normalize(source_without_quotes):
        errors.append("脚本未完整覆盖母稿，或包含母稿之外的内容")

    # Emotion must be read off the guide; a plain 'neutral' on a guided line means
    # the delivery was never decided, which is exactly what flattens the TTS output.
    emotion_counts: dict[str, int] = {}
    for index, segment in enumerate(segments, 1):
        if segment.get("type") != "dialogue":
            continue
        emotion = str(segment.get("emotion") or DEFAULT_DIALOGUE_EMOTION)
        emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
        if emotion != DEFAULT_DIALOGUE_EMOTION:
            continue
        guide, _ = _guide_segment(segments, index - 1)
        if not guide or not _has_speech_verb(guide):
            continue
        if any(keyword in guide for keyword, _ in _EMOTION_LOOKUP):
            continue  # guide does carry an emotion cue; a miss is a rule gap, not text
        warnings.append(
            f"{segment.get('segment_id')}: 台词情绪为 neutral，前置旁白也没有神态词"
            f"（如“笑着说”“拍着手喊”），这句会用平淡语气念出来"
        )

    total_chars = narrator_chars + dialogue_chars
    ratio = narrator_chars / total_chars if total_chars else 0.0
    policy = script.get("policy") or {}
    if policy != expected_policy:
        errors.append("音频形式政策已更新，必须按当前旁白/对白规则重新编译")
    min_ratio = float(expected_policy["narrator_min_ratio"])
    max_ratio = float(expected_policy["narrator_max_ratio"])
    # 硬门放宽到 min_ratio 的一半：脚本层已经把能转述的对白转述了，剩下的对白
    # 是故事本身的体裁定型（角色本来就话多）。低于这个底线说明素材确实不适合做
    # 音频故事，值得让故事层重写；在底线之上只是"不够理想"，出提示即可。
    hard_floor = min_ratio * 0.5
    if ratio < hard_floor:
        errors.append(
            f"旁白占比过低：{ratio:.1%}，低于底线 {hard_floor:.0%}"
            f"（目标 {min_ratio:.0%}）——这个故事对白太重，不适合做成音频故事"
        )
    elif ratio < min_ratio:
        warnings.append(
            f"旁白占比偏低：{ratio:.1%}，目标 {min_ratio:.0%}。"
            f"脚本层已尽力转述，仍偏低说明故事本身对白偏多"
        )
    if ratio > max_ratio:
        warnings.append(f"旁白占比偏高：{ratio:.1%}，建议不超过 {max_ratio:.0%}")
    max_allowed_streak = int(expected_policy["max_consecutive_dialogue"])
    if max_streak > max_allowed_streak:
        # 连续台词只是听感问题（对话密集的桥段本来就该你一句我一句），
        # 说话人由改编层确定、正文由程序保证，这里没有"配错音"的风险，
        # 所以降为提示，不再拦截流程。
        warnings.append(
            f"有 {max_streak} 段台词连着出现（建议不超过 {max_allowed_streak} 段），"
            f"听感上略拥挤"
        )

    full_text = "".join(reconstructed)
    errors.extend(validate_fact_text(full_text, facts))
    if require_approved:
        approval = script.get("approval") or {}
        if script.get("status") != "approved":
            errors.append("音频脚本尚未人工批准")
        elif approval.get("digest") != approval_digest(script):
            errors.append("批准后脚本发生变化，必须重新审核批准")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "segments": len(segments),
            "narrator_segments": sum(1 for s in segments if s.get("type") == "narrator"),
            "dialogue_segments": sum(1 for s in segments if s.get("type") == "dialogue"),
            "narrator_chars": narrator_chars,
            "dialogue_chars": dialogue_chars,
            "narrator_ratio": round(ratio, 4),
            "max_consecutive_dialogue": max_streak,
            "dialogue_emotions": dict(sorted(emotion_counts.items())),
            "neutral_dialogue_segments": emotion_counts.get(DEFAULT_DIALOGUE_EMOTION, 0),
        },
    }


def assert_valid(script: dict, require_approved: bool = False) -> dict:
    report = validate_audio_script(script, require_approved=require_approved)
    if not report["ok"]:
        detail = "\n".join(f"- {item}" for item in report["errors"])
        raise AudioScriptError(f"音频脚本质量门未通过:\n{detail}")
    return report


def approve_script(script: dict) -> dict:
    report = assert_valid(script, require_approved=False)
    script["qa"] = report
    script["status"] = "approved"
    script["approval"] = {
        "digest": approval_digest(script),
        "qa_metrics": report["metrics"],
    }
    return script


def to_storyboard(script: dict) -> dict:
    assert_valid(script, require_approved=True)
    scenes = [{
        "id": 1,
        "segment_id": "title",
        "speaker": "旁白",
        "narration": script["title"],
        "emotion": "narrate",
        "keywords": [script["title"]],
        "title": True,
    }]
    for index, segment in enumerate(script["segments"], 2):
        scenes.append({
            "id": index,
            "segment_id": segment["segment_id"],
            "speaker": segment["speaker"],
            "narration": segment["text"],
            "emotion": segment.get("emotion") or ("narrate" if segment["type"] == "narrator" else "neutral"),
            "keywords": [],
            "title": False,
        })
    return {
        "title": script["title"],
        "story_id": re.sub(r"[^\w-]", "", script["title"]) or "audio_story",
        "style": "audio",
        "bgm": {},
        "scenes": scenes,
        "audio_script_digest": approval_digest(script),
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _print_report(report: dict) -> None:
    metrics = report["metrics"]
    print(f"QA: {'通过' if report['ok'] else '失败'} | "
          f"{metrics['segments']} 段 | 旁白 {metrics['narrator_ratio']:.1%} | "
          f"台词 {metrics['dialogue_segments']} 段")
    for item in report["errors"]:
        print(f"  ERROR: {item}")
    for item in report["warnings"]:
        print(f"  WARN: {item}")


def main() -> None:
    parser = argparse.ArgumentParser(description="纯音频故事脚本编译与质量门")
    sub = parser.add_subparsers(dest="command", required=True)

    compile_cmd = sub.add_parser("compile", help="从已确认母稿确定性编译音频脚本")
    compile_cmd.add_argument("--story", required=True)
    compile_cmd.add_argument("--facts", required=True)
    compile_cmd.add_argument("--out", required=True)

    validate_cmd = sub.add_parser("validate", help="重新验证脚本、母稿与事实表")
    validate_cmd.add_argument("--script", required=True)
    validate_cmd.add_argument("--require-approved", action="store_true")

    approve_cmd = sub.add_parser("approve", help="人工复核后冻结当前脚本版本")
    approve_cmd.add_argument("--script", required=True)

    args = parser.parse_args()
    if args.command == "compile":
        script = compile_audio_script(args.story, args.facts)
        report = validate_audio_script(script)
        script["qa"] = report
        _print_report(report)
        if not report["ok"]:
            raise SystemExit(1)
        out = _resolve_path(args.out)
        _write_json(out, script)
        print(f"已生成草稿: {out}")
    elif args.command == "validate":
        script_path = _resolve_path(args.script)
        report = validate_audio_script(_read_json(script_path), require_approved=args.require_approved)
        _print_report(report)
        if not report["ok"]:
            raise SystemExit(1)
    elif args.command == "approve":
        script_path = _resolve_path(args.script)
        script = approve_script(_read_json(script_path))
        _write_json(script_path, script)
        print(f"已批准并冻结: {script_path}")


if __name__ == "__main__":
    main()

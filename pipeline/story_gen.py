"""Direct story writer used by Studio and the command line."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from .config import ROOT, load_config
from .llm_client import chat_text

# 长度：圣经《十六、长度与节奏》定"点子目标 600~800 字，500~900 是硬范围"。
# 之前这里按音频时长反推成"500 字、±10%"，比圣经更紧，等于用通用条款压住了圣经。
STORY_TARGET_MIN, STORY_TARGET_MAX = 600, 800
STORY_HARD_MIN, STORY_HARD_MAX = 500, 900
STORY_WRITE_ATTEMPTS = 3      # 字数不达标时重写次数
STORY_REVIEW_ATTEMPTS = 3     # 轻量质检不过时重写次数
# 写故事的单次调用超时。客户端默认 180 秒，但长故事实测要 1.5~9 分钟，
# 默认值会把请求掐在生成中途，返回空内容（报错信息完全看不出是超时）。
STORY_LLM_TIMEOUT = 600
LIGHT_REVIEW_SYSTEM = (
    "你是儿童故事的轻量校对员。只检查四件事：开头是否交代了孩子能听懂的人物和情境；"
    "数字、年龄和因果关系是否说清楚且算得通；是否出现明显违反日常常识的陈述；"
    "结尾是否能理解。夸张的童言童语可以保留，但必须看得出是在开玩笑。"
    "不要评价文风，不要提出改写方向。只输出 JSON："
    '{"pass":true,"issues":[]} 或 {"pass":false,"issues":["具体问题"]}。没有问题时 issues 必须为空数组。'
)
MATERIAL_TYPES = {"auto", "idea", "incomplete", "complete"}
STORY_TYPES = {"auto", "family", "imagination"}
STORY_TYPE_ALIASES = {"fantasy": "imagination"}
SERIES_BIBLE_PATH = ROOT / "stories" / "牛牛一家人_儿童故事创作圣经_V1.0.md"  # 牛牛系列的圣经（由系列设置引用）


def detect_material_type(text: str) -> str:
    compact = re.sub(r"\s+", "", text or "")
    if compact.endswith(("…", "...", "……")):
        return "incomplete"
    punctuation = re.findall(r"[。！？.!?]", compact)
    if len(compact) <= 60:
        return "complete" if len(punctuation) >= 3 and compact[-1:] in "。！？.!?" else "idea"
    return "complete" if compact[-1:] in "。！？.!?\"”’" else "incomplete"


def resolve_material_type(value: str, text: str) -> str:
    selected = (value or "auto").strip().lower()
    if selected not in MATERIAL_TYPES:
        raise ValueError(f"未知输入类型: {value}")
    return detect_material_type(text) if selected == "auto" else selected


def detect_story_type(text: str, style_guide: str = "") -> str:
    compact = re.sub(r"\s+", "", text or "")
    imagination_terms = ("变成", "巨人", "魔法", "仙女", "会飞", "飞到", "月球", "外星",
                         "穿越", "缩小", "变大", "梦见", "梦里", "会说话", "变身", "隐身", "奇幻", "异世界")
    if any(term in compact for term in imagination_terms):
        return "imagination"
    family_terms = ("妈妈", "爸爸", "姐姐", "哥哥", "妹妹", "弟弟", "家里", "床上", "厨房", "客厅",
                    "吃饭", "睡觉", "洗澡", "刷牙", "玩具", "上学", "幼儿园", "扮演", "比赛", "做饭")
    if any(term in compact for term in family_terms):
        return "family"
    if any(term in (style_guide or "") for term in ("家庭日常", "一家人", "家庭小故事")):
        return "family"
    return "imagination"


def normalize_story_type(value: str) -> str:
    selected = STORY_TYPE_ALIASES.get((value or "auto").strip().lower(), (value or "auto").strip().lower())
    if selected not in STORY_TYPES:
        raise ValueError(f"未知故事类型: {value}")
    return selected


def resolve_story_type(value: str, text: str, style_guide: str = "") -> str:
    selected = normalize_story_type(value)
    return detect_story_type(text, style_guide) if selected == "auto" else selected


def _series_bible_context(style_guide: str = "", cast: dict | None = None,
                          series_bible: str = "") -> str:
    """读取本系列的故事圣经。它是故事层【唯一】的创作依据，读不到就必须报错。

    以前这里有三处问题（都已修）：
      1. 只有"看起来像牛牛系列"时才加载圣经，其它系列静默降级；
      2. 文件缺失/路径写错时返回空串，故事会凭通用提示词裸写；
      3. 圣经被当成"附加参考"贴在提示词末尾，地位低于通用条款。

    注意：**不提供默认文件名**。早期版本把"没配圣经"当成"那就用牛牛那本"，
    结果是另一个系列会悄悄套用牛牛的人物和世界观——写出来的故事串味且无人察觉。
    现在没配就直接报错，让配置问题当场暴露。
    """
    requested = (series_bible or "").strip()
    if not requested:
        raise ValueError(
            "这个系列没有配置故事圣经。故事层必须依据圣经创作——"
            "请在「系列」设置里填写圣经文件路径。"
        )
    path = Path(requested)
    if not path.is_absolute():
        path = ROOT / path
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(
            f"读不到系列故事圣经（{path}）：{exc}。"
            f"故事层必须依据圣经创作，请在「系列」里检查圣经路径。"
        ) from exc
    if not text:
        raise ValueError(f"系列故事圣经是空文件（{path}）")
    return text


def build_story_facts(facts: dict | None, source_text: str, material_type: str,
                      story_type: str, cast: dict | None = None) -> dict:
    """Preserve explicit production facts; never infer creative constraints."""
    supplied = facts or {}
    merged = {key: list(supplied.get(key) or [])
              for key in ("characters", "world_facts", "required_patterns", "forbidden_patterns")}
    existing = {row if isinstance(row, str) else row.get("name", "") for row in merged["characters"]}
    merged["characters"].extend({"name": name, "aliases": []}
                                for name in (cast or {}) if name and name not in existing)
    merged["story_type"] = STORY_TYPE_ALIASES.get(story_type, story_type)
    return merged


def _generate(llm_cfg: dict, system: str, user: str) -> str:
    cfg = dict(llm_cfg)
    # max_tokens 必须显式给足。不设的话走服务商默认值，而一篇 600~800 中文字的故事
    # 加上 JSON 包装会超过它，返回就被**截断**：表现为"模型没有返回任何内容"或
    # "未输出合法 JSON"，而一次写故事要 3~9 分钟且是付费调用——这个坑很难从报错看出来。
    # 600~800 中文字约合 1200~2000 token，留足余量取 4096。
    cfg.setdefault("max_tokens", 4096)
    # 超时也要给足。客户端默认 180 秒，而 kimi-k3 写一篇长故事实测 1.5~9 分钟：
    # 默认值会让请求在**生成中途**被掐断、返回空内容，报错却只是"模型没有返回任何内容"，
    # 完全看不出是超时所致（踩过：连续三次空返回，白等 9 分钟）。
    cfg["timeout"] = float(cfg.get("timeout") or STORY_LLM_TIMEOUT)
    return chat_text(cfg, system, user, response_format={"type": "json_object"}).strip()


def _parse_story_payload(text: str) -> dict:
    """把模型输出解析成 {"title":..., "text":...}。

    模型并不总是老老实实返回 JSON：长文生成时常见三种走样——
      1. 正常 JSON；
      2. JSON 外面套了一段说明文字（"好的，这是故事：{...}"）；
      3. 干脆只返回正文纯文本（标题用 # 或第一行）。
    第 3 种以前直接判"模型未输出合法 JSON"整条作废，而一次写故事要 3~7 分钟、
    还是付费调用——所以这里必须能兜住：正文能用就用，别让格式问题毁掉整条任务。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("模型没有返回任何内容")
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I).strip()

    # 1) 直接解析
    payload = None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # 2) 从夹杂说明的文字里抠出最外层 JSON 对象
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                payload = None
    if isinstance(payload, dict):
        title = str(payload.get("title") or "").strip().strip('"“”「」')
        body = str(payload.get("text") or payload.get("story") or "").strip()
        if body:
            return {"title": title, "text": body}

    # 3) 纯文本兜底：找 "# 标题" 行，找不到就把整段当正文
    lines = [ln.strip() for ln in cleaned.splitlines()]
    title = ""
    body_lines = lines
    for i, ln in enumerate(lines):
        if not ln:
            continue
        if ln.startswith("#"):
            title = ln.lstrip("#").strip().strip('"“”「」')
            body_lines = lines[i + 1:]
        break   # 只看第一个非空行：是标题就取走，不是就当正文开头
    body = "\n".join(ln for ln in body_lines).strip()
    if not body:
        raise ValueError("模型返回的内容里找不到故事正文")
    return {"title": title or "未命名故事", "text": body}


def _load_json_object(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型未输出合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("故事输出必须是 JSON 对象")
    return payload


def _light_story_review(llm_cfg: dict, idea: str, title: str, body: str) -> list[str]:
    """Run one small common-sense check; a review service failure is non-blocking."""
    user = (
        "用户点子：\n" + (idea or "").strip() +
        "\n\n故事标题：\n" + title.strip() +
        "\n\n故事正文：\n" + body.strip()
    )
    try:
        payload = _load_json_object(_generate(llm_cfg, LIGHT_REVIEW_SYSTEM, user))
    except Exception:
        return []
    if payload.get("pass") is not False:
        return []
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        return ["故事存在需要修正的常识或可理解性问题"]
    issues = [item.strip() for item in raw_issues
              if isinstance(item, str) and item.strip()]
    return issues[:4] or ["故事存在需要修正的常识或可理解性问题"]


def story_char_range(material_type: str = "idea") -> tuple[int, int]:
    """故事的合格字数区间（含端点）。

    点子类按圣经的"目标 600~800"；较完整的稿子以忠实保留原篇幅为先，
    所以用圣经的硬范围 500~900，避免为了凑字数扩写。
    """
    if material_type == "idea":
        return STORY_TARGET_MIN, STORY_TARGET_MAX
    return STORY_HARD_MIN, STORY_HARD_MAX


def _length_clause(material_type: str) -> str:
    """字数要求的具体说法：完整故事优先忠实原篇幅，不硬凑。"""
    if material_type == "idea":
        return (f"一句话点子请扩展成 {STORY_TARGET_MIN}~{STORY_TARGET_MAX} 字"
                f"（硬范围 {STORY_HARD_MIN}~{STORY_HARD_MAX} 字）。")
    return (f"较完整的稿子优先忠实保留原篇幅和情节，不要为凑字数扩写；"
            f"正文落在 {STORY_HARD_MIN}~{STORY_HARD_MAX} 字之间。")


# 全局提示词只留【所有儿童故事都成立】的底线：输出格式、事实自洽、孩子听得懂。
# 系列特有的东西（人物、世界观、风格、开头/结尾/幽默/长度节奏）全部由圣经规定，
# 所以这里刻意不重复——重复会让通用条款压过圣经。
GENERAL_RULES = (
    "1. 事实自洽：数字、年龄、次数、时间关系要说清楚并且算得通"
    "（例如「5 岁加上 12 次等于 17 岁」这种玩笑，必须把起始值、次数、结果写明）。\n"
    "2. 可以用明显是玩笑的夸张，但不要把违反日常常识的事当成事实。\n"
    "3. 用语要让学龄前孩子听得懂：短句、口语、具体，不要抽象概念和大段科普。\n"
    "4. 只输出故事本身，不要输出分析、说明或修改记录。"
)


def _build_story_system(context: str, material_type: str) -> str:
    """组装写故事的提示词：圣经在前（主体），通用底线在后（格式与自洽）。"""
    return (
        "你是儿童故事作者。下面这份【系列故事创作圣经】规定了人物、世界观、"
        "风格、开头与结尾、幽默与情绪、长度与节奏——它是你创作的主要依据，"
        "请严格按它来写。\n\n"
        "===== 系列故事创作圣经（主要创作依据）=====\n"
        f"{context}\n"
        "===== 圣经结束 =====\n\n"
        "以下是适用于所有儿童故事的通用底线，与圣经不冲突时一并遵守：\n"
        f"{GENERAL_RULES}\n\n"
        "【本次任务的额外要求】\n"
        "- 输出格式：只输出一个 JSON 对象，形如 "
        '{"title":"故事标题","text":"故事正文"}，不要任何解释。\n'
        f"- 长度：{_length_clause(material_type)}\n"
        "- 本环节只负责写出好听的故事，不需要考虑配音、旁白与对白比例、"
        "台词引导语等音频形式问题（这些由后续改编环节处理）。"
    )


def _simple_story_package(llm_cfg: dict, source_text: str, *, style_guide: str = "",
                          cast: dict | None = None, series_bible: str = "",
                          facts: dict | None = None, material_type: str = "idea",
                          story_type: str = "family", original_text: str = "",
                          feedback: str = "", enforce_quality: bool = True) -> dict:
    context = _series_bible_context(style_guide, cast, series_bible)
    low, high = story_char_range(material_type)
    system = _build_story_system(context, material_type)
    user = "用户点子：\n" + (source_text or "").strip()
    if original_text:
        user += "\n\n上一版故事：\n" + original_text.strip()
    if feedback:
        user += "\n\n用户修改要求：\n" + feedback.strip()
    user += f"\n\n请直接写出完整故事。{_length_clause(material_type)}"

    def write_candidate(extra_feedback: list[str] | None = None) -> tuple[str, str]:
        request = user
        if extra_feedback:
            request += "\n\n上一版存在以下问题，请重写故事并修正：\n- " + "\n- ".join(extra_feedback)
        payload = _parse_story_payload(_generate(llm_cfg, system, request))
        candidate_title = str(payload.get("title") or "").strip().strip('"“”「」')
        candidate_body = str(payload.get("text") or payload.get("story") or "").strip()
        if not candidate_title or not candidate_body:
            raise ValueError("模型未返回 title 或 text")
        return candidate_title, candidate_body

    # 1) 字数：允许 ±10%，不达标就把差距写清楚让模型重写
    title = body = ""
    for attempt in range(1, STORY_WRITE_ATTEMPTS + 1):
        try:
            if attempt == 1:
                title, body = write_candidate()
            else:
                previous = len(re.sub(r"\s+", "", body))
                gap = "太长" if previous > high else "太短"
                title, body = write_candidate([
                    f"上次正文 {previous} 字，{gap}了。请把正文控制在 {low}~{high} 字之间，"
                    f"保持故事情节完整，{'精简描写' if previous > high else '补充动作和对话细节'}。"
                ])
        except (RuntimeError, ValueError) as exc:
            print(f"  [story] 第 {attempt} 次写故事失败：{exc}")
            if attempt >= STORY_WRITE_ATTEMPTS:
                raise
            # 模型偶发返回空/坏 JSON（超时截断、网关抽风）也值得再试一次：
            # 这类失败与故事质量无关，直接判死会让整条出片任务白跑。
            continue
        visible = len(re.sub(r"\s+", "", body))
        if low <= visible <= high:
            break
        print(f"  [story] 第 {attempt} 次 {visible} 字（目标 {low}~{high}），重写…")
    visible = len(re.sub(r"\s+", "", body))
    if visible < STORY_HARD_MIN:
        raise ValueError(
            f"故事太短（当前{visible}字，至少要 {STORY_HARD_MIN} 字）")
    if visible > STORY_HARD_MAX:
        raise ValueError(
            f"故事太长（当前{visible}字，上限 {STORY_HARD_MAX} 字），"
            f"已重写 {STORY_WRITE_ATTEMPTS} 次仍不达标")

    # 2) 质量：轻量质检不过就带着意见重写
    if enforce_quality:
        for attempt in range(1, STORY_REVIEW_ATTEMPTS + 1):
            issues = _light_story_review(llm_cfg, source_text, title, body)
            if not issues:
                break
            print(f"  [story] 第 {attempt} 次轻量质检不过，带意见重写：{issues[:1]}")
            if attempt >= STORY_REVIEW_ATTEMPTS:
                raise ValueError("故事未通过轻量质检：" + "；".join(issues))
            # 重写本身也可能撞上模型抽风（返回空/坏格式）。以前这里一失败就整条作废，
            # 而"质检提了意见"恰恰说明这稿只差一点——必须给重写留重试机会。
            rewritten = None
            for rewrite_try in range(1, STORY_WRITE_ATTEMPTS + 1):
                try:
                    rewritten = write_candidate(issues)
                    break
                except (RuntimeError, ValueError) as exc:
                    print(f"  [story] 重写第 {rewrite_try} 次失败：{exc}")
                    if rewrite_try >= STORY_WRITE_ATTEMPTS:
                        raise ValueError(
                            f"故事未通过轻量质检，且重写 {STORY_WRITE_ATTEMPTS} 次都失败：{exc}"
                        ) from exc
            title, body = rewritten
            visible = len(re.sub(r"\s+", "", body))
            if visible < STORY_HARD_MIN:
                raise ValueError(
                    f"重写后故事太短（当前{visible}字，至少要 {STORY_HARD_MIN} 字）")
    return {
        "title": title, "text": body,
        "story_facts": build_story_facts(facts, source_text, material_type, story_type, cast),
        "story_digest": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "story_pipeline": "simple",
    }


def generate_story_package(llm_cfg: dict, source_text: str, *, cast: dict | None = None,
                           style_guide: str = "", series_bible: str = "", facts: dict | None = None,
                           material_type: str = "auto", story_type: str = "auto",
                           brief: dict | None = None, outline: dict | None = None,
                           original_text: str = "", feedback: str = "", enforce_quality: bool = True) -> dict:
    del brief, outline
    return _simple_story_package(
        llm_cfg, source_text, cast=cast, style_guide=style_guide, series_bible=series_bible,
        facts=facts, material_type=resolve_material_type(material_type, source_text),
        story_type=resolve_story_type(story_type, source_text, style_guide),
        original_text=original_text, feedback=feedback, enforce_quality=enforce_quality,
    )


def gen_story(llm_cfg: dict, theme: str, framework: str = "", cast: dict | None = None,
              style_guide: str = "", series_bible: str = "", facts: dict | None = None,
              material_type: str = "auto", story_type: str = "auto") -> tuple[str, str]:
    package = generate_story_package(llm_cfg, theme, cast=cast, style_guide=style_guide or framework,
                                     series_bible=series_bible, facts=facts,
                                     material_type=material_type, story_type=story_type)
    return package["title"], package["text"]


def rewrite_story(llm_cfg: dict, theme: str, feedback: str, original_text: str = "",
                  facts: dict | None = None, material_type: str = "complete", story_type: str = "auto",
                  cast: dict | None = None, style_guide: str = "", series_bible: str = "",
                  brief: dict | None = None, outline: dict | None = None, source_text: str = "") -> tuple[str, str]:
    package = generate_story_package(llm_cfg, source_text or original_text or theme, cast=cast,
                                     style_guide=style_guide, series_bible=series_bible, facts=facts,
                                     material_type=material_type, story_type=story_type,
                                     original_text=original_text, feedback=feedback)
    return package["title"], package["text"]


def _safe_id(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]", "", title) or "story"


def write_story(theme: str, framework: str, llm_cfg: dict, mode: str = "accept", extra: str = "",
                facts: dict | None = None, story_type: str = "auto") -> tuple[str, str, str, str]:
    if mode in {"rewrite", "edit"}:
        title, body = rewrite_story(llm_cfg, theme, extra, original_text=theme, facts=facts,
                                    material_type="complete", story_type=story_type, style_guide=framework)
    else:
        title, body = gen_story(llm_cfg, theme, framework, facts=facts, story_type=story_type)
    sid = _safe_id(title)
    drafts = ROOT / "stories" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / f"{sid}_v{len(list(drafts.glob(f'{sid}_v*.md'))) + 1}.md"
    path.write_text(f"{title}\n{body}", encoding="utf-8")
    return title, body, sid, str(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成儿童故事")
    parser.add_argument("--theme", required=True)
    parser.add_argument("--framework", default="")
    parser.add_argument("--material-type", choices=sorted(MATERIAL_TYPES), default="auto")
    parser.add_argument("--story-type", choices=sorted(STORY_TYPES | {"fantasy"}), default="auto")
    args = parser.parse_args()
    title, body = gen_story(load_config().get("llm", {}), args.theme, args.framework,
                            material_type=args.material_type, story_type=args.story_type)
    print(f"# {title}\n\n{body}")


if __name__ == "__main__":
    main()

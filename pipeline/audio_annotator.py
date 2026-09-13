"""脚本改编层：把已定稿的故事母稿改造成"能配音的脚本"。

职责划分（本模块存在的理由）：
  - 【故事层】只负责好看、精彩、符合系列圣经，不承担任何音频形式要求。
  - 【本层】负责改编：断句、判断说话人、决定对白还是旁白、控制旁白比例。
  - 【程序层】只做机械校验：分段拼回去必须逐字等于母稿、说话人必须在人物表里。

**为什么断句也交给模型**：断句本来就是"改编"的一部分。以前程序用"超过 90 字就在
任意位置切开"的规则断句，结果切出半句话（"……跳上沙发" / "地跳上沙发"），
听众听到重复；为堵这个洞又加了"只在句末切""引号归谁""助词并入前段"等一串补丁，
每次补丁又引出新问题。规则断句本身就是那个洞——所以整段交给模型，程序只验拼接。
"""
from __future__ import annotations

import json
import re

from .llm_client import chat_text

# 台词情绪交给模型判断——它正在读整篇，上下文（前因后果、人物性格）都看得见。
# 这里列出的都是配音引擎（豆包 V3 / qwen）真正支持的情绪指令，
# 不要凭空发明：写错的情绪会让配音退化成平淡朗读。
EMOTIONS = (
    "happy", "excited", "laugh", "triumph", "proud", "brave",      # 正向
    "scared", "anxious", "surprise", "cry", "sad", "angry",        # 负向
    "question", "mystery", "whisper", "awkward", "tender", "gentle", "sleepy", "neutral",
)

SEGMENT_SYSTEM = (
    "你是儿童音频故事的【配音脚本编辑】。下面给你一篇已定稿的故事母稿，"
    "它已经被切分成编号的片段。\n"
    "你的工作是逐条判断：**每个片段该由旁白念，还是由某个角色说；如果是角色说的，"
    "他当时是什么语气**。\n\n"
    "对每一个编号输出一条判断：\n"
    "  - 由旁白念：{\"index\":0,\"type\":\"narrator\"}\n"
    "  - 由角色说：{\"index\":1,\"type\":\"dialogue\",\"speaker\":\"角色名\","
    "\"emotion\":\"情绪\"}\n\n"
    "判断规则：\n"
    "  1. 【编号必须齐全】每个编号都要出现，从 0 到最后一个，不重不漏。\n"
    "  2. 【speaker 只能用人物表里的名字】，一个字都不能改。判断看上下文语义，"
    "不要依赖固定句式：即使引导语写在台词后面、或用动作代替「说」，也要判断正确。"
    "几个人一起说的，写其中一个代表人物。\n"
    "  3. 【引号片段】以引号开头结尾的片段就是那句话本身，判断它是谁说的、什么语气；"
    "引导语所在的片段（例如「爸爸拍拍胸脯说：」）属于旁白。\n"
    "  4. 【不在引号里的内容都是旁白】。\n\n"
    "关于 emotion（**只给台词写，这是配音的语气，很重要**）：\n"
    "  从下面这些词里选一个，不要自己造词：\n"
    "    " + "、".join(EMOTIONS) + "\n"
    "  判断依据是**上下文**，不是台词字面：\n"
    "    - 看前面发生了什么：刚被吓到、刚赢了比赛、刚做错事被发现，语气都不一样；\n"
    "    - 看谁在说：5 岁的牛牛和爸爸、妈妈的语气本来就不一样；\n"
    "    - 看这句话要做什么：炫耀、撒娇、逞强、解释、提醒、明知故问，各有对应情绪。\n"
    "  参考：\n"
    "    逞强装勇敢 → brave；被吓到 → scared；急着要结果 → anxious；\n"
    "    得意炫耀 → triumph；偷偷说秘密 → whisper 或 mystery；\n"
    "    明知答案还问 → question；闹了小笑话不好意思 → awkward；\n"
    "    哄人、安慰 → tender；笑着说 → laugh 或 happy。\n"
    "  **不要整篇都给 neutral**：neutral 只在真的没有情绪时才用"
    "（例如平静地陈述事实）。一篇故事里大多数台词都该有明显的语气。\n\n"
    "输出 JSON：{\"items\":[{\"index\":0,\"type\":\"narrator\"},"
    "{\"index\":1,\"type\":\"dialogue\",\"speaker\":\"爸爸\","
    "\"emotion\":\"brave\"}, …]}\n"
    "只输出 JSON，不要任何解释。"
)


def _build_user(sentences: list[str], cast_names: list[str]) -> str:
    lines = [
        "人物表（speaker 只能用这些名字）：",
        "、".join(cast_names) or "（空）",
        "",
        "故事母稿的编号句子：",
    ]
    for i, text in enumerate(sentences):
        lines.append(f"[{i}] {text.strip()}")
    lines += ["", f"请把这些编号句子分组成配音脚本，输出 JSON。"
                  f"（编号 0 到 {len(sentences) - 1} 每个都要用一次）"]
    return "\n".join(lines)


def _extract_json(out: str) -> dict:
    text = (out or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            return json.loads(text[start:end + 1])
        raise ValueError("改编结果不是 JSON 对象")


def plan_script(llm_cfg: dict, sentences: list[str], cast_names: list[str]
                ) -> tuple[dict[int, dict], str]:
    """让模型**逐条判断每个片段**是旁白还是谁说的。

    返回 ``(items, error)``，``items`` 形如 ``{片段编号: {"type":..., "speaker":...}}``，
    覆盖全部编号。``error`` 非空表示这次改编不可用，调用方回退到规则编译。

    **为什么是"逐条判断"而不是"分组"**：实测让模型把编号分组成段落时，它会写出
    互相重叠、覆盖不全的分组（旁白段横跨了全部片段），再怎么加校验都拼不出脚本。
    改成逐条选择题后，模型只需回答"这一段谁念"，任务变简单，结构由程序保证：
    正文按片段区间取出，**重复或漏字在结构上不可能发生**。
    """
    if not sentences:
        return {}, "没有可改编的片段"
    valid = {str(n) for n in cast_names}
    try:
        out = chat_text(llm_cfg, SEGMENT_SYSTEM, _build_user(sentences, cast_names))
        data = _extract_json(out)
    except Exception as exc:
        return {}, f"改编调用失败：{exc}"

    raw = data.get("items")
    if not isinstance(raw, list) or not raw:
        return {}, "改编结果缺少 items"

    items: dict[int, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= len(sentences):
            continue
        kind = str(entry.get("type") or "").strip()
        if kind == "dialogue":
            speaker = str(entry.get("speaker") or "").strip()
            if speaker not in valid:
                continue          # 名字不在人物表里 → 这一条不采纳，让它当旁白
            record = {"type": "dialogue", "speaker": speaker}
            # 情绪只认配音引擎支持的那几种，写错就退回 neutral（由装配层再兜一次）
            emotion = str(entry.get("emotion") or "").strip().lower()
            if emotion in EMOTIONS:
                record["emotion"] = emotion
            items[index] = record
        elif kind == "narrator":
            items[index] = {"type": "narrator"}

    if not items:
        return {}, "改编结果里没有可用的判断"
    if len(items) < len(sentences) * 0.6:
        return {}, f"改编只判断了 {len(items)}/{len(sentences)} 个片段，覆盖太少"
    return items, ""

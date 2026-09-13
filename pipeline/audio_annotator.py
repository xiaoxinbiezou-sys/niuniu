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


SCENE_SYSTEM = (
    "你是儿童绘本的【场景统筹】。下面是一部故事音频的分镜旁白（按播放顺序编号）。\n"
    "请为每一镜判断**故事发生的具体地点**，并写出现场环境。\n\n"
    "对每一镜输出：\n"
    "  {\"id\":1,\"location\":\"小区花坛边\",\"environment\":\"傍晚的小区花坛，矮灌木和泥土，旁边有楼房\"}\n\n"
    "要求：\n"
    "  1. 【location 是地点名】4~8 个字，具体到能画出来"
    "（「小区花坛边」而不是「外面」；「厨房灶台前」而不是「家里」）。\n"
    "  2. 【同一地点必须用完全相同的写法】"
    "如果第 2 镜和第 5 镜都在同一个地方，两镜的 location 要**逐字相同**"
    "（写成「小区花坛边」就都写「小区花坛边」，不要一个写「花坛」一个写「小区花坛」）。\n"
    "  3. 【按旁白和原文判断，不要一律当成室内】"
    "故事可能在小区、公园、马路边、幼儿园、超市、奶奶家……"
    "旁白说「在小区花坛边发现」就要用小区，不要因为主角是小孩就默认在家里。"
    "只有旁白确实发生在室内（床、沙发、厨房、浴缸等）才写室内地点。\n"
    "  3b.【注意地点会变】故事原文里可能出现转折"
    "（「把它带回家」「第二天」「回到房间」），"
    "地点变了就要跟着改；后面的镜子如果回到同一个地方，写回原来那个名字。\n"
    "  4. 【environment 只写环境】房间/场地的布局、家具或植物、地面、窗外，"
    "**不要写人物、不要写光线时间、不要写情绪**，一句话 20~40 字。\n"
    "  5. 同一地点在不同镜里的 environment 也要写成一样的一句，便于跨镜一致。\n\n"
    "只输出 JSON：{\"scenes\":[{\"id\":1,\"location\":\"…\",\"environment\":\"…\"}, …]}\n"
    "不要任何解释。"
)


def plan_scenes(llm_cfg: dict, narrations: list[str], story_body: str = "") -> tuple[list[dict], str]:
    """让模型逐镜判断故事发生的地点与环境。

    返回 ``(scenes, error)``，``scenes`` 与 ``narrations`` 等长，每项
    ``{"location":..., "environment":...}``；出错时返回空列表，调用方回退到关键词规则。

    **必须把故事原文一起给它**：只给"每一镜的旁白"时，模型看不到全局——
    实测一篇"在楼下花坛边发现麻雀宝宝"的故事，后半段其实已经把它带回家照顾了
    （原文里有"我给它盖被子""接待站关门"），但只看单镜旁白判断不出这个转折，
    结果整篇都判成花坛边。

    **为什么不能让程序猜**：原来用一张 6 条的关键词表匹配场景，匹配不到就一律
    回落到「家里客厅」，而且表里根本没有户外场景——一篇发生在"小区花坛边"的故事
    整篇配图都被画进了室内。地点是语义判断，正该由读得到全文的模型来做。
    """
    if not narrations:
        return [], "没有可判断的分镜"
    lines = [f"[{i + 1}] {t.strip()}" for i, t in enumerate(narrations)]
    user = ""
    if story_body.strip():
        user += ("故事原文（用来把握整体场景走向，注意地点可能中途改变）：\n"
                 + story_body.strip() + "\n\n")
    user += ("画面对应的分镜旁白（按播放顺序，这就是要判断的场景顺序）：\n"
             + "\n".join(lines)
             + f"\n\n请为这 {len(narrations)} 镜逐个判断地点和环境，输出 JSON。"
               f"（id 从 1 到 {len(narrations)}）")
    try:
        data = _extract_json(chat_text(llm_cfg, SCENE_SYSTEM, user))
    except Exception as exc:
        return [], f"场景判断调用失败：{exc}"

    raw = data.get("scenes")
    if not isinstance(raw, list) or not raw:
        return [], "场景判断结果缺少 scenes"
    by_id: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        location = str(item.get("location") or "").strip()
        environment = str(item.get("environment") or "").strip()
        if 1 <= index <= len(narrations) and location:
            by_id[index] = {"location": location, "environment": environment}
    if len(by_id) < len(narrations) * 0.6:
        return [], f"场景判断只覆盖 {len(by_id)}/{len(narrations)} 镜"
    return [by_id.get(i + 1, {}) for i in range(len(narrations))], ""


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

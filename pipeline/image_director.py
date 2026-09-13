"""图片策划层（M3.5）：分镜生成后、生图之前，为每镜产出统一详细的图片规格。

职责（v2 重构，解决"场景不一致"根因）：
  场景由【分镜师】在 scene 字段定死（源头全知），图片编辑【不猜场景、不改场景】，
  只做两件事：
  1. 【场景基准卡】：每个唯一场景生成一段基准环境描述（scene_bases），
     同场景所有镜【逐字复用】同一段环境描述 → 场景视觉跨镜一致（程序级复用，非提示词恳求）；
  2. 【画面完善统一】：每镜在基准场景之上补充人物数量/道具颜色/服装/动作细节，
     人物数量明确、道具颜色全片统一、服装按 costume 字段。

纠错权：若 scene 字段与旁白/常识冲突（如旁白说"冲下楼去比赛"但 scene 填"家里客厅"），
  图片编辑【必须纠正】scene 并在输出标注 scene_fixed（分镜师可能填错，不锁死）。

流程：
  阶段A：LLM 读全部分镜 → 输出 {scene_bases: {场景名: 基准环境描述}, scenes: [{scene_id, scene, scene_fixed?, image_prompt}]}
  阶段B（程序级）：写回时把 scene_bases[scene] 以固定前缀拼入 image_prompt（保证逐字一致）
"""
from __future__ import annotations

import json
import re

from .llm_client import chat_text

# 阶段A：一次调用，通盘设计全部画面（这是保证跨图一致的关键：
# 分开单张生成时，道具尺寸和人物数量每次都会被模型重新猜一遍）
DIRECTOR_SYSTEM = (
    "你是儿童绘本的【画面导演】。你会一次看到全部分镜，请【通盘考虑、一次设计完所有画面】，"
    "让它们在人物长相、服装、道具外观和尺寸上像同一本绘本里画出来的。\n"
    "你要输出三部分：\n"
    "  1. props：把全片反复出现的关键道具【定义一次】（铁罐/浇水壶/种子/绿芽等）。"
    "每个道具写清楚：颜色、材质、形状，以及【相对人物的尺寸】——"
    "一定要写成'只有小男孩手掌那么大''到他的小腿一半高'这种相对说法，"
    "禁止只写绝对厘米数（模型会把道具怼在镜头前拍成巨大一个）；\n"
    "  2. scene_bases：每个场景写一段【固定环境描述】（房间布局/家具/地面/窗外），"
    "禁止写人物、禁止写光线时间；\n"
    "  3. scenes：每一镜写一条可直接生图的 image_prompt。\n"
    "每条 image_prompt 的硬性要求：\n"
    "  - 【人数写死】以'画面里共有且仅有 N 个人物'开头，把在场角色逐个列出并写明【每个角色各一个】"
    "（如'1 个小男孩（牛牛）、1 个爸爸'），"
    "在场角色只能用提供的称呼：小男孩(牛牛)/小女孩(添添)/爸爸/妈妈，禁止写角色名当画面内容。"
    "只写总人数不够——实测会画出两个爸爸，必须逐个角色写数量；\n"
    "  - 【只画一个瞬间】只表现该镜的这一个动作，不要把整段旁白都画进去；\n"
    "  - 【道具逐字复用】用到 props 里的道具时，把该道具的定义【原样抄进这条描述】，"
    "禁止改写、禁止省略尺寸；这一镜没用到的道具就不要提；\n"
    "  - 【尺寸按相对比例写】禁止只写绝对厘米数，必须写成'只有小男孩手掌那么大'"
    "'再到他的小腿一半高'这类相对人物的说法——绝对数字会被理解错，"
    "同一个铁罐会在两张图里画成一个手掌大、一个水桶大；\n"
    "  - 【场景逐字复用】环境描写从 scene_bases 原样抄入；\n"
    "  - 【禁止画面文字】台词是拿去配音和上字幕的，"
    "禁止把台词、旁白、标题或任何文字写进画面；结尾统一加'画面里不要出现任何文字'；\n"
    "  - 【服装一致】日常服装以系列固定角色形象为准；只有剧情明确换装时才改；\n"
    "  - 每条以'9:16竖版，无字幕，无水印，不要拼图，不要分镜格'结尾。\n"
    "只输出一个 JSON 对象，格式：\n"
    '{"props":{"铁罐":"银色金属小茶叶罐，只有小男孩手掌那么大，侧面光滑没有商标"},'
    '"scene_bases":{"家里客厅":"客厅环境描述"},'
    '"scenes":[{"scene_id":1,"scene":"家里客厅","scene_fixed":"","image_prompt":"画面里共有且仅有…"}]}\n'
    "不要输出其他内容。\n"
)


def _build_user(sb: dict, cast: dict | None = None) -> str:
    lines = []
    for sc in sb.get("scenes", []):
        present = "、".join(sc.get("present") or [sc.get("character", "")]) or "（未注明）"
        lines.append(
            f"镜{sc['id']}: 在场={present} | 场景={sc.get('scene','（未注明）')} "
            f"| 服装={sc.get('costume','')} | 这一镜的画面={sc.get('narration','')}"
        )
    user = (f"共有 {len(sb.get('scenes', []))} 镜，请一次为全部 {len(sb.get('scenes', []))} 镜"
            f"设计画面，并保证跨镜一致。\n\n分镜表：\n" + "\n".join(lines))
    if cast:
        cast_lines = "\n".join(f"- {k}：{v}" for k, v in cast.items())
        user += f"\n\n【系列固定角色（长相以此为准，不要改）】\n{cast_lines}"
    return user


def _parse_result(out: str) -> dict:
    """解析 {scene_bases, scenes} JSON（容忍输出前后杂讯）。"""
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def direct_images(sb: dict, llm_cfg: dict, cast: dict | None = None) -> dict:
    """为分镜每镜生成统一强化版 image_prompt。返回 {scene_id: image_prompt}。

    同时把 scene_bases 写回 sb["scene_bases"]（供后续复用）。
    失败时返回空 dict（调用方应保留原 prompt 兜底）。
    """
    if not sb.get("scenes"):
        return {}
    user = _build_user(sb, cast)
    try:
        out = chat_text(llm_cfg, DIRECTOR_SYSTEM, user)
        data = _parse_result(out)
    except Exception as e:
        print(f"  [image_director] ⚠️ 图片策划失败: {e}")
        return {}

    scene_bases = data.get("scene_bases") or {}
    props = data.get("props") or {}
    scenes_out = data.get("scenes") or []
    if not scenes_out:
        return {}

    # 场景基准卡与道具定义写回 sb（同场景/同道具跨镜复用的实体）
    sb["scene_bases"] = scene_bases
    sb["props"] = props
    # 记录导演纠正后的场景（不覆盖 sc["scene"]：那是"家里客厅"这样的地点名，
    # 覆盖它会让前端和成片把整段环境描写当成场景标签显示）
    for item in scenes_out:
        if isinstance(item, dict) and item.get("scene_id") is not None and item.get("scene_fixed"):
            for sc in sb["scenes"]:
                if sc["id"] == int(item["scene_id"]):
                    sc["scene_fixed"] = item["scene_fixed"]

    # 阶段B（程序级）：把"场景基准 + 道具尺寸"这些跨图约束统一挪到描述**末尾**。
    # 实测写在中间的尺寸说明会被模型忽略（同一个铁罐在两张图里一个手掌大、一个水桶大），
    # 而模型对提示词结尾的约束最敏感。
    _CONSTRAINTS = ("9:16竖版，无字幕，无水印，不要拼图，不要分镜格")
    prompts: dict[int, str] = {}
    for item in scenes_out:
        if not isinstance(item, dict) or item.get("scene_id") is None:
            continue
        sid = int(item["scene_id"])
        ip = (item.get("image_prompt") or "").strip()
        if not ip:
            continue
        sc = next((s for s in sb["scenes"] if s["id"] == sid), None)
        # 导演可能漏抄道具定义，程序化补在末尾；已经在描述里写过的不重复加
        narration = (sc or {}).get("narration", "")
        used = [f"{name}：{spec}" for name, spec in props.items()
                if isinstance(spec, str) and name and name in narration and spec not in ip]
        tails = []
        # 去掉描述里已有的结尾套话，避免同一句出现两次
        for marker in ("9:16竖版", "画面里不要出现任何文字"):
            cut = ip.find(marker)
            if cut > 0:
                ip = ip[:cut].rstrip("。；;，, ")
        if used:
            tails.append("【道具尺寸，必须与其它画面完全一致】" + "；".join(used))
        tails.append("画面里不要出现任何文字。")
        scene_name = (item.get("scene_fixed") or item.get("scene")
                      or (sc or {}).get("scene", ""))
        base = (scene_bases.get(scene_name) or "").strip()
        if base:
            tails.append(f"【固定环境】{base}。")
        # 人数放在最后再说一遍：写在中间时模型会画出"两个爸爸"
        present = (sc or {}).get("present") or []
        if present:
            ordered = [n for n in ("牛牛", "添添", "爸爸", "妈妈") if n in present]
            ordered += [n for n in present if n not in ordered]
            nouns = {"牛牛": "小男孩", "添添": "小女孩", "爸爸": "爸爸", "妈妈": "妈妈"}
            counts = "、".join(f"1个{nouns.get(n, n)}" for n in ordered)
            tails.append(f"【人物数量不许变】画面里只能有：{counts}，一个都不能多画。")
        tails.append(_CONSTRAINTS)
        prompts[sid] = ip + "".join(tails)
    return prompts


def apply_director(sb: dict, llm_cfg: dict, cast: dict | None = None) -> bool:
    """把图片策划结果写回 sb 的每镜 image_prompt。返回是否成功。"""
    prompts = direct_images(sb, llm_cfg, cast)
    if not prompts:
        return False
    n = 0
    for sc in sb["scenes"]:
        p = prompts.get(sc["id"])
        if p:
            sc["image_prompt"] = p
            n += 1
    print(f"[M3.5 图片策划] 已强化 {n}/{len(sb['scenes'])} 镜的画面描述"
          + (f"（{len(sb.get('scene_bases') or {})} 个场景基准卡）" if sb.get("scene_bases") else ""))
    return n > 0

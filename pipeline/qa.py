"""M3 图片自动质检。

用视觉模型做两件独立的事，一次调用完成：

1. 【角色一致性】生成图里的角色，长相/发型/服装是否与角色定妆照一致；
2. 【画面忠实度】画面内容有没有和这一镜的旁白矛盾
   —— 例如旁白说"打开一看，里面只有泥土"，画面却长出了草莓。

结果用 JSON 返回并严格解析。判定式写错会让"不一致"被当成"通过"，而调用失败
（没配 key、网络问题）又会被当成"内容不一致"，于是白白重画三次再报错——这两种
情况必须区分开。
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

QA_MODEL = "qwen-vl-max"
QA_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

CHECKED = "checked"          # 真正跑完了检查（通过或不通过）
PASS = "pass"
FAIL = "fail"
UNAVAILABLE = "unavailable"  # 没 key / 调用失败：不能当成"不一致"
UNCLEAR = "unclear"          # 模型没给出可判读的结论：既不算通过也不算不通过

_CONSISTENT = "consistent"
_INCONSISTENT = "inconsistent"
_UNKNOWN = "unknown"


def _b64(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()


def _qa_key(cfg: dict) -> str:
    """质检用视觉模型的 key：优先设置中心 llm.qwen，其次 config.yaml llm / tts.qwen。"""
    key = ""
    try:
        from studio import store
        s = store.get_settings()
        key = (s.get("llm", {}).get("providers", {}).get("qwen", {}) or {}).get("api_key", "") or ""
    except Exception:
        pass
    if not key:
        key = cfg.get("llm", {}).get("api_key", "") or ""
    if not key:
        key = cfg.get("tts", {}).get("qwen", {}).get("api_key", "") or ""
    return key


def _parse_verdict(ans: str) -> tuple[str, str, str, str]:
    """(角色判定, 忠实度判定, 角色理由, 矛盾对象)。

    每个判定是 consistent / inconsistent / unknown。`faithful` 判 false 时还会取
    `contradiction`（画面里那个不该出现的东西）——判不通过必须说得出具体是什么，
    否则判定会在边界上抖动，导致无谓重画。
    """
    text = (ans or "").strip()
    if text.startswith("```") or "{" in text:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                payload = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):

                def read(key: str) -> str:
                    value = payload.get(key)
                    if value is True:
                        return _CONSISTENT
                    if value is False:
                        return _INCONSISTENT
                    return _UNKNOWN

                return (read("consistent"), read("faithful"),
                        str(payload.get("reason") or ""),
                        str(payload.get("contradiction") or "").strip())
    if not text:
        return _UNKNOWN, _UNKNOWN, "", ""

    def judge(ok_token: str, bad_token: str) -> str:
        if bad_token in text:
            return _INCONSISTENT
        if ok_token in text:
            return _CONSISTENT
        return _UNKNOWN

    return (judge("一致:是", "一致:否"), judge("忠实", "不忠实"), text, "")


def check_image(ref_paths: list[str], img_path: str, cfg: dict, timeout: int = 120,
                narration: str = "") -> dict:
    """检查一张生成图。

    返回 {"status": checked|unavailable, "ok": bool, "reason": str}。
    status == unavailable 表示**没能检查**（缺 key / 调用失败），调用方不应据此重画。
    """
    if not Path(img_path).exists():
        return {"status": CHECKED, "ok": False, "reason": "生成图不存在"}

    key = _qa_key(cfg)
    if not key:
        return {"status": UNAVAILABLE, "ok": False,
                "reason": "未配置质检 key（设置中心 → 模型 → 千问），无法确认画面质量"}

    parts = []
    for index, ref in enumerate([p for p in (ref_paths or []) if Path(p).exists()][:4], 1):
        parts.append({"type": "image_url", "image_url": {"url": _b64(Path(ref))},
                      "text": f"参考图{index}：角色定妆照"})
    parts.append({"type": "image_url", "image_url": {"url": _b64(Path(img_path))},
                  "text": "待检图"})
    n_ref = len([p for p in parts if "定妆照" in p.get("text", "")])

    if n_ref:
        ask = (f"前 {n_ref} 张是角色定妆照，最后一张是儿童绘本待检图。只回答两个问题：\n"
               f"1) 待检图里出现的主要角色，是否都能在定妆照中找到同一个人"
               f"（脸型、发型、服装一致）？只要有一个对不上就算 false。\n")
    else:
        ask = "最后一张是儿童绘本待检图。\n1) consistent 一律填 true（本镜无定妆照可比对）。\n"
    if narration.strip():
        ask += (f"2) 【只抓硬矛盾】这一镜的旁白按顺序讲了这些事：「{narration.strip()}」。\n"
                f"   请按旁白的时间顺序核对画面：有没有出现【在旁白这个时间点还不该存在】的东西？"
                f"注意：旁白里提到的东西，不一定此刻就已经存在。"
                f"例如旁白说“第二天才买来种子”“刚把种子埋进土里”，"
                f"那么此刻就不该已经长出苗、更不该出现成熟的果子；"
                f"旁白说“打开一看只有泥土”，画面就不该有草莓。\n"
                f"   以下都【不算】矛盾，faithful 填 true："
                f"画面只画了旁白里的某一个瞬间、没有把每句话都画出来；"
                f"表情与文字描述的情绪不完全一致；光线、家具摆设等细节；"
                f"种子的具体颜色形状与读者想象略有差异。\n")
    else:
        ask += "2) faithful 一律填 true（本镜没有旁白可比对）。\n"
    ask += ('只输出 JSON：{"consistent":true/false,"faithful":true/false,'
            '"contradiction":"画面里那个不该出现的东西，没有就留空","reason":"一句话中文说明"}')
    parts.append({"type": "text", "text": ask})

    body = {
        "model": QA_MODEL,
        "messages": [{"role": "user", "content": parts}],
        "temperature": 0,
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        QA_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # 网络/鉴权问题 → 没能检查，不是"画面不一致"
        return {"status": UNAVAILABLE, "ok": False, "reason": f"质检调用失败：{exc}"}

    ans = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
    consistent, faithful, reason, contradiction = _parse_verdict(ans)
    if _UNKNOWN in (consistent, faithful):
        return {"status": UNCLEAR, "ok": False,
                "reason": f"质检模型没有给出可判读的结论：{ans.strip()[:80]!r}", "answer": ans.strip()}
    problems = []
    warnings = []
    if consistent == _INCONSISTENT:
        # 角色长相是客观比对，错了就重画
        problems.append("角色与定妆照不一致")
    if faithful == _INCONSISTENT:
        if contradiction:
            # 事实矛盾只当警告，不阻断：视觉模型对"此刻该不该出现"的判断在边界上会抖动
            # （同一张图两次判定可能相反），拿它拦住整条流水线会让人没法出片。
            warnings.append(f"画面可能和旁白矛盾：{contradiction}（{reason[:50]}）")
        else:
            warnings.append(f"质检拿不准（{reason[:60]}）")
    return {"status": CHECKED, "ok": not problems,
            "reason": "；".join(problems) or "通过", "warnings": warnings, "answer": ans.strip()}


def check_image_strict(ref_paths: list[str], img_path: str, cfg: dict,
                       max_attempts: int = 3, gen_fn=None, scene=None,
                       narration: str = "") -> tuple[bool, int, str]:
    """带重试的质检：检查 → 不通过 → 重画 → 再检查。

    gen_fn：重画回调，签名 gen_fn(scene, out_path)。
    返回 (最终是否通过, 尝试次数, 原因)。缺 key / 调用失败时**不重画**，
    直接返回失败并说明原因——以前这种情况会白白重画三次然后报"连续三次不一致"。
    """
    reason = ""
    for attempt in range(1, max_attempts + 1):
        verdict = check_image(ref_paths, str(img_path), cfg, narration=narration)
        if verdict["status"] in (UNAVAILABLE, UNCLEAR):
            return False, attempt, verdict["reason"]
        if verdict["ok"]:
            for note in verdict.get("warnings") or []:
                print(f"  [qa] {note}")
            return True, attempt, verdict["reason"]
        reason = verdict["reason"]
        if attempt < max_attempts and gen_fn is not None:
            print(f"  [qa] 第 {attempt} 次不合格（{reason}），重画 {Path(img_path).name} …")
            Path(img_path).unlink(missing_ok=True)
            gen_fn(scene, img_path)
    return False, max_attempts, reason


if __name__ == "__main__":
    # 手动测试：python -m pipeline.qa <生成图> <参考图1> [参考图2...]
    from .config import load_config
    refs = sys.argv[2:]
    result = check_image(refs, sys.argv[1], load_config())
    print(f"质检结果: {result['status']} / {'通过 ✅' if result['ok'] else '不通过 ❌'} — {result['reason']}")

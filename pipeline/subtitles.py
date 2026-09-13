"""M5 字幕模块：按分镜时间轴生成 ASS 大字幕（抖音风：白字黑边 + 关键词高亮）。"""
from __future__ import annotations

from pathlib import Path

from .animate import _scene_duration


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 100))
    h, rem = divmod(ms, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _wrap_tagged(text: str, limit: int) -> str:
    """按可见字符数换行（忽略 ASS 内联标签），插入 \\N。"""
    out = []
    count = 0
    i = 0
    while i < len(text):
        if text[i] == "{":
            j = text.find("}", i)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i])
        count += 1
        if count >= limit:
            out.append(r"\N")
            count = 0
        i += 1
    return "".join(out)


def _highlight(text: str, keywords: list[str], hl_color: str) -> str:
    for kw in keywords:
        if not kw:
            continue
        idx = text.find(kw)
        if idx >= 0:
            text = f"{text[:idx]}{{\\c{hl_color}}}{kw}{{\\c}}{text[idx + len(kw):]}"
    return text


def generate_ass(storyboard: dict, audios: list[dict], cfg: dict,
                 out_path: str = "output/subs/final.ass",
                 offset: float = 0.0) -> str:
    sub_cfg = cfg["subtitle"]
    W = int(cfg["project"]["width"])
    H = int(cfg["project"]["height"])
    font_name = sub_cfg.get("font_name", "Microsoft YaHei")
    font_size = int(H * float(sub_cfg.get("font_size_ratio", 0.045)))
    margin_v = int(sub_cfg.get("margin_bottom", 260))
    primary = sub_cfg.get("primary_color", "&H00FFFFFF")
    outline = sub_cfg.get("outline_color", "&H00000000")
    hl = sub_cfg.get("highlight_color", "&H0000F6FF")
    wrap = int(sub_cfg.get("wrap_chars", 11))

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {W}",
        f"PlayResY: {H}",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_name},{font_size},{primary},{primary},{outline},&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,20,20,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    t = offset
    for sc, audio in zip(storyboard["scenes"], audios):
        dur = _scene_duration(audio)
        start, end = t, t + audio["duration"]
        text = _highlight(sc["narration"], sc.get("keywords", []), hl)
        text = _wrap_tagged(text, wrap)
        lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text}")
        t += dur

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[M5 字幕] {len(audios)} 条 → {out}")
    return str(out)

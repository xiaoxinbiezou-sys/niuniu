"""M8 封面图：多模板渲染，模板文字可改写。

模板定义：covers/templates.json（4 套：classic/storybook/cartoon/minimal）
文字字段（可按模板配置）：badge / title / subtitle / tagline，前端可编辑。

用法：
  python -m pipeline.cover --storyboard stories/storyboard.xxx.json --template cartoon
  python -m pipeline.cover --storyboard ... --template classic --text "badge=我的频道|title=自定义标题"
（make_video 末尾也会自动生成）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import ROOT, load_config

W, H = 1080, 1920
FONT = "C:/Windows/Fonts/msyhbd.ttc"


def load_templates() -> list[dict]:
    p = ROOT / "covers" / "templates.json"
    return json.load(open(p, encoding="utf-8"))["templates"]


def get_template(tid: str) -> dict:
    for t in load_templates():
        if t["id"] == tid:
            return t
    return load_templates()[0]


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, size)


def _wrap(text: str, max_chars: int = 5) -> list[str]:
    """标题断行。

    优先【对半拆】而不是贪心填满：7 个字的标题按每行 5 个字贪心会断成
    「铁罐里的草 / 莓种子」，把"草莓"劈开了；对半拆成「铁罐里 / 的草莓种子」更好读，
    两行长短也更接近，居中排版更稳。
    """
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    if len(text) <= max_chars * 2:
        cut = (len(text) + 1) // 2
        return [text[:cut], text[cut:]]
    # 超过两行的：按需要的行数均分
    line_count = (len(text) + max_chars - 1) // max_chars
    size = (len(text) + line_count - 1) // line_count
    return [text[i:i + size] for i in range(0, len(text), size)]


def _gradient_darken(img, top=0.45, bottom=0.62):
    w, h = img.size
    alphas = []
    for y in range(h):
        t = y / h
        a = 0
        if t < top:
            a = int(190 * (1 - t / top))
        elif t > bottom:
            a = int(210 * ((t - bottom) / (1 - bottom)))
        alphas.append(a)
    grad = Image.new("L", (1, h))
    grad.putdata(alphas)
    grad = grad.resize((w, h))
    return Image.composite(Image.new("RGB", (w, h), (12, 14, 26)), img, grad)


def _pill(img, cx, cy, text, font, fill=(255, 214, 130, 235), tcolor=(80, 52, 18, 255)):
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=font)
    pw, ph = int(tw + 56), 76
    pill = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pd.rounded_rectangle([0, 0, pw, ph], radius=ph // 2, fill=fill)
    pd.text((pw // 2, ph // 2), text, font=font, fill=tcolor, anchor="mm")
    img.paste(pill, (int(cx - pw / 2), int(cy - ph / 2)), pill)


def _title_block(img, lines, font_size, start_y, stroke=6):
    d = ImageDraw.Draw(img)
    lh = int(font_size * 1.3)
    for i, line in enumerate(lines):
        y = start_y + i * lh
        d.text((W // 2 + 5, y + 6), line, font=_font(font_size), fill=(0, 0, 0, 160), anchor="mm")
        d.text((W // 2, y), line, font=_font(font_size), fill=(255, 255, 255, 255),
               anchor="mm", stroke_width=stroke, stroke_fill=(30, 24, 40))


def _fit_title_single_line(title: str, max_font: int, fill: float = 0.8,
                           min_font: int = 60) -> int:
    """按"标题排成一行时能占满画面宽度的 fill"反推字号（不实际换行）。"""
    if not title:
        return max_font
    probe = _font(100)
    width_at_100 = probe.getbbox(title)[2] - probe.getbbox(title)[0]
    if width_at_100 <= 0:
        return max_font
    return int(max(min_font, min(max_font, W * fill * 100 / width_at_100)))


def _render_classic(img, title, texts, wrap_chars=5):
    d = ImageDraw.Draw(img)
    _pill(img, W // 2, 220, texts["badge"], _font(42))
    lines = _wrap(title, wrap_chars)
    fs = int(min(170, (W * 0.78) / max(len(l) for l in lines)))
    _title_block(img, lines, fs, 780 - (len(lines) - 1) * int(fs * 1.3) // 2)
    if texts.get("subtitle"):
        d.text((W // 2, 1640), texts["subtitle"], font=_font(40), fill=(235, 235, 240, 210), anchor="mm")


def _render_storybook(img, title, texts, wrap_chars=5):
    d = ImageDraw.Draw(img)
    # 白色描边画框
    m = 70
    d.rectangle([m, m, W - m, H - m], outline=(255, 255, 255, 200), width=10)
    d.rectangle([m + 26, m + 26, W - m - 26, H - m - 26], outline=(255, 255, 255, 90), width=3)
    _pill(img, W // 2, 250, texts["badge"], _font(40))
    lines = _wrap(title, wrap_chars)
    fs = int(min(150, (W * 0.72) / max(len(l) for l in lines)))
    _title_block(img, lines, fs, 800 - (len(lines) - 1) * int(fs * 1.3) // 2)
    if texts.get("subtitle"):
        d.text((W // 2, 1560), texts["subtitle"], font=_font(38), fill=(250, 245, 235, 220), anchor="mm")


def _render_cartoon(img, title, texts, wrap_chars=5):
    d = ImageDraw.Draw(img)
    # 左上角彩色横幅徽章
    bf = _font(40)
    tw = d.textlength(texts["badge"], font=bf)
    d.polygon([(90, 150), (90 + tw + 70, 150), (90 + tw + 40, 150 + 74), (90, 150 + 74)],
              fill=(255, 214, 130, 240))
    d.text((90 + tw / 2 + 15, 150 + 37), texts["badge"], font=bf, fill=(80, 52, 18, 255), anchor="mm")

    # 标题：整块在画面垂直正中。字号按"排成一行占满画面宽度"反推，
    # 再按实际行数补偿 1/行数 —— 目标是把标题排成 2 行，而不是挤成一行超宽。
    lines = _wrap(title, wrap_chars)
    probe = _font(160)
    w160 = probe.getbbox(title)[2] - probe.getbbox(title)[0]
    fs = max(70, min(160, int(W * 0.82 * 160 / max(w160, 1))))
    if len(lines) == 2:
        fs = int(fs * 1.8)
    fs = min(fs, 190)
    lh = int(fs * 1.32)
    pad = 44
    block_h = len(lines) * lh + pad * 2
    top = (H - block_h) // 2
    bar = Image.new("RGBA", (W - 160, block_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    bd.rounded_rectangle([0, 0, bar.width, bar.height], radius=40, fill=(255, 255, 255, 232))
    img.paste(bar, (80, top), bar)
    _title_block(img, lines, fs, top + pad + (len(lines) - 1) * lh // 2, stroke=4)
    if texts.get("tagline"):
        # 标语放在标题条下方，且与条底留出间距（原来会压在条上）
        d.text((W // 2, top + block_h + 80), texts["tagline"], font=_font(42),
               fill=(255, 255, 255, 235), anchor="mm", stroke_width=3, stroke_fill=(30, 24, 40))


def _render_minimal(img, title, texts, wrap_chars=4):
    d = ImageDraw.Draw(img)
    _pill(img, 130, 130, texts["badge"], _font(34), fill=(255, 255, 255, 165), tcolor=(60, 45, 90, 255))
    lines = _wrap(title, wrap_chars)
    fs = int(min(185, (W * 0.7) / max(len(l) for l in lines)))
    _title_block(img, lines, fs, 800 - (len(lines) - 1) * int(fs * 1.3) // 2, stroke=7)
    # 细线点缀
    d.line([(W // 2 - 160, 1100), (W // 2 + 160, 1100)], fill=(255, 255, 255, 200), width=3)
    if texts.get("subtitle"):
        d.text((W // 2, 1720), texts["subtitle"], font=_font(36), fill=(235, 235, 240, 200), anchor="mm")


RENDERERS = {
    "classic": _render_classic,
    "storybook": _render_storybook,
    "cartoon": _render_cartoon,
    "minimal": _render_minimal,
}


def generate_cover(storyboard: dict, cfg: dict, bg_path: str | None = None,
                   out_path: str = "", template_id: str = "classic",
                   texts: dict | None = None) -> str:
    """生成封面。texts 可覆盖模板默认文字（badge/title/subtitle/tagline）。"""
    cover_cfg = cfg.get("cover", {})
    tpl = get_template(template_id)
    t = dict(tpl["defaults"])
    if texts:
        t.update({k: v for k, v in texts.items() if v is not None})
    title = t.get("title") or storyboard.get("title", "故事")

    if bg_path and Path(bg_path).exists():
        bg = Image.open(bg_path).convert("RGB").resize((W, H), Image.LANCZOS)
    else:
        sys.exit("[cover] 缺少背景图（bg_path）")
    bg = _gradient_darken(bg).filter(ImageFilter.GaussianBlur(1.2))

    RENDERERS.get(template_id, _render_classic)(bg, title, t)

    out = Path(out_path) if out_path else ROOT / "output" / "covers" / f"{template_id}_{storyboard.get('story_id', 'story')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out, "PNG")
    print(f"[M8 封面] 模板[{template_id}] 《{title}》 → {out.relative_to(ROOT)}")
    return str(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="封面图模板生成")
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--bg", default="")
    ap.add_argument("--template", default="classic", choices=[t["id"] for t in load_templates()])
    ap.add_argument("--text", default="", help='文字覆盖，如 "badge=我的频道|subtitle=晚安故事"')
    ap.add_argument("--out", default="")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    p = Path(args.storyboard)
    if not p.is_absolute():
        p = ROOT / p
    sb = json.load(open(p, encoding="utf-8"))
    bg = args.bg or (ROOT / "output" / "images" / sb["story_id"] / "scene_01.png")
    texts = {}
    for seg in args.text.split("|"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            texts[k.strip()] = v.strip()
    generate_cover(sb, cfg, str(bg) if Path(bg).exists() else None, args.out, args.template, texts)


if __name__ == "__main__":
    main()

"""M3 文生图模块：按分镜生成 9:16 竖屏画面。

backend 支持：
  placeholder —— 程序化占位画面（水彩风渐变天空+月亮+星星+山影），零依赖，先跑通全流程
  local       —— 本地 SDXL/FLUX（需 NVIDIA GPU，待接入）
  api         —— 云图片 API（需 key，待接入）
"""
from __future__ import annotations

import io
import json
import random
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


# ------------------------------------------------------- placeholder ----
def _gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        d.line([(0, y), (w, y)], fill=(r, g, b))
    return img


def _moon(draw, cx, cy, r, color=(255, 241, 200)):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    # 月晕
    for k in range(3, 0, -1):
        alpha = 18 - k * 5
        if alpha <= 0:
            continue
        ring = Image.new("RGBA", (r * 6, r * 6), (0, 0, 0, 0))
        rd = ImageDraw.Draw(ring)
        rr = r + k * 14
        rd.ellipse([r * 3 - rr, r * 3 - rr, r * 3 + rr, r * 3 + rr],
                   outline=(255, 241, 180, alpha), width=2)
        draw._image.paste(ring, (cx - r * 3, cy - r * 3), ring)


def _stars(draw, w, h, n, rng, color=(255, 255, 255)):
    for _ in range(n):
        x, y = rng.randint(0, w), rng.randint(0, int(h * 0.55))
        s = rng.choice([1, 1, 2, 2, 3])
        draw.ellipse([x, y, x + s, y + s], fill=color)


def _hills(draw, w, h, base_y, color, rng):
    pts = [(0, h)]
    x = 0
    while x < w:
        x += rng.randint(120, 260)
        pts.append((x, base_y + rng.randint(-40, 40)))
    pts.append((w, h))
    draw.polygon(pts, fill=color)


def _fox_silhouette(img, rng):
    """简单小狐狸剪影（占位用，后续由 AI 图替换）。"""
    w, h = img.size
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    color = (212, 140, 70, 255)
    x = int(w * 0.5)
    y = int(h * 0.72)
    body_r = int(w * 0.11)
    d.ellipse([x - body_r, y - body_r, x + body_r, y + body_r], fill=color)          # 身体
    d.ellipse([x - int(body_r * 0.55), y - int(body_r * 1.5),
               x + int(body_r * 0.55), y - int(body_r * 0.4)], fill=color)           # 头
    d.polygon([(x - int(body_r * 0.5), y - int(body_r * 1.3)),
               (x - int(body_r * 1.0), y - int(body_r * 1.9)),
               (x - int(body_r * 0.2), y - int(body_r * 1.25))], fill=color)         # 左耳
    d.polygon([(x + int(body_r * 0.5), y - int(body_r * 1.3)),
               (x + int(body_r * 1.0), y - int(body_r * 1.9)),
               (x + int(body_r * 0.2), y - int(body_r * 1.25))], fill=color)         # 右耳
    d.ellipse([x - int(body_r * 1.25), y + int(body_r * 0.9),
               x + int(body_r * 1.25), y + int(body_r * 2.6)], fill=color)           # 尾巴
    img.paste(overlay, (0, 0), overlay)
    return img


def _gen_placeholder(scene: dict, cfg: dict, out_path: Path, rng: random.Random) -> None:
    w = int(cfg["project"]["width"])
    h = int(cfg["project"]["height"])
    night_palettes = [
        ((24, 32, 84), (96, 120, 190)),   # 深蓝夜空
        ((20, 28, 70), (120, 96, 170)),   # 紫蓝
        ((30, 40, 90), (150, 110, 160)),  # 紫粉
        ((16, 24, 60), (80, 130, 180)),   # 蓝青
    ]
    top, bottom = rng.choice(night_palettes)
    img = _gradient((w, h), top, bottom)
    d = ImageDraw.Draw(img)
    _stars(d, w, h, rng.randint(120, 180), rng)
    moon_present = scene["id"] in (1, 2, 3, 4, 5, 9) or rng.random() < 0.4
    if moon_present:
        _moon(d, int(w * 0.5), int(h * 0.22), int(w * 0.16))
    _hills(d, w, h, int(h * 0.62), (34, 46, 96), rng)
    _hills(d, w, h, int(h * 0.7), (26, 36, 78), rng)
    _hills(d, w, h, int(h * 0.8), (18, 26, 60), rng)
    # 小狐狸剪影：远景镜头出现，特写镜头不出现
    if scene["id"] in (1, 3, 5, 6, 7, 9):
        _fox_silhouette(img, rng)
    # 场景 8 画一个发光窗台
    if scene["id"] == 8:
        d.rectangle([int(w * 0.32), int(h * 0.62), int(w * 0.68), int(h * 0.82)],
                    fill=(80, 60, 40))
        d.rectangle([int(w * 0.37), int(h * 0.66), int(w * 0.63), int(h * 0.78)],
                    fill=(255, 214, 130))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(out_path, "PNG")


# --------------------------------------------------------------- api ----
def _cover_resize(img, w: int, h: int) -> Image.Image:
    """等比缩放并居中裁切到目标尺寸。"""
    ratio = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


# 角色指代 → 参考图键（与 settings image.refs 的角色名对应）
# 角色指代/名字 → 参考图键。既要匹配"图中的这个小男孩"式指代，也要匹配"牛牛""添添"式裸名
_ROLE_REF_KEY = {
    "这个小男孩": "牛牛",
    "这个小女孩": "添添",
    "这个爸爸": "爸爸",
    "这个妈妈": "妈妈",
    "这一家人": "旁白",
    "小男孩": "牛牛",
    "小女孩": "添添",
    "牛牛": "牛牛",
    "添添": "添添",
    "姐姐": "添添",
    "爸爸": "爸爸",
    "妈妈": "妈妈",
}

def _collect_refs(scene: dict, refs: dict) -> tuple[list[str], list[str]]:
    """从 scene 的 image_prompt 解析角色 → (参考图路径列表, 角色键列表)，两者顺序一致。

    识别两类写法：指代词（"图中的这个小男孩"）和角色名（"牛牛""添添""妈妈"）。
    同一角色多次出现只算一次；scene.character 兜底（prompt 完全没写时用）。
    服装状态：scene.get("costume")（如 armor）→ 对应角色换用战甲参考图。
    """
    prompt = scene.get("image_prompt", "")
    costume = scene.get("costume", "")
    # 收集所有命中的 (位置, 角色键)
    hits = []  # (pos, role)
    seen_roles = set()
    for k, role in _ROLE_REF_KEY.items():
        pos = prompt.find(k)
        if pos >= 0 and role not in seen_roles:
            hits.append((pos, role))
            seen_roles.add(role)
    hits.sort(key=lambda x: x[0])
    # 全家 vs 单人：prompt 同时写了"这一家人"和 2+ 个单人角色名时，丢弃全家福用单人参考图
    #（单人图信息更细，避免全家福+4张单人重复引用）
    if "旁白" in seen_roles:
        singles = [r for r in seen_roles if r in ("牛牛", "添添", "爸爸", "妈妈")]
        if len(singles) >= 2:
            seen_roles.discard("旁白")
            hits = [(pos, role) for pos, role in hits if role != "旁白"]
    # character 兜底：prompt 没识别到任何角色，或主角角色不在 prompt 里
    char_role = scene.get("character", "")
    if char_role in ("牛牛", "添添", "爸爸", "妈妈") and char_role not in seen_roles:
        hits.append((10 ** 9, char_role))
        seen_roles.add(char_role)
    order = [role for _, role in hits]
    paths = []
    valid_order = []
    for role in order:
        key = f"{role}_{costume}" if costume else role
        rp = refs.get(key) or refs.get(role) or refs.get("全家")
        if rp and Path(rp).exists():
            paths.append(rp)
            valid_order.append(role)
    if not valid_order:
        rp = refs.get("全家")
        if rp and Path(rp).exists():
            return [rp], []
    return paths, valid_order


def _ref_template(prompt: str, ref_paths: list[str] | None = None,
                  order: list[str] | None = None) -> str:
    """参考图生成模板：显式写"参考图中的…保持…一致"，否则 API 忽略参考图。

    多参考图：按 order（角色键出现顺序）编号"图一/图二/…"，与 ref_paths 一一对应。
    prompt 中所有角色写法（指代或裸名）统一替换为"图N中的<角色名>"。
    """
    import re as _re

    p = prompt.strip()
    n = len(ref_paths or [])
    if n >= 2 and order:
        role_no = {role: idx + 1 for idx, role in enumerate(order)}
        # 角色名 → 该角色在 prompt 里的各种写法（长指代优先）
        role_aliases = {
            "牛牛": ["这个小男孩", "小男孩", "牛牛", "this小男孩"],
            "添添": ["这个小女孩", "小女孩", "添添", "姐姐", "this小女孩"],
            "爸爸": ["这个爸爸", "爸爸", "this爸爸"],
            "妈妈": ["这个妈妈", "妈妈", "this妈妈"],
            "旁白": ["这一家人", "一家人", "this一家人"],
        }
        # 别名 → 图号（替换成占位符，最后统一还原，避免二次替换污染）
        alias_no = {}
        for role, aliases in role_aliases.items():
            if role not in role_no:
                continue
            for a in aliases:
                alias_no[a] = role_no[role]
        aliases_sorted = sorted(alias_no, key=len, reverse=True)
        pat = "|".join(_re.escape(a) for a in aliases_sorted)
        # 第一轮：图中[的]?<别名> → §图号§
        p2 = _re.sub(r"图中[的]?(" + pat + ")", lambda m: f"§{alias_no[m.group(1)]}§", p)
        # 第二轮：剩余裸名 <别名> → §图号§（替换后占位符无别名，不会二次污染）
        p2 = _re.sub("(" + pat + ")", lambda m: f"§{alias_no[m.group(1)]}§", p2)
        # 还原占位符 → "图N中的角色名"
        role_by_no = {v: k for k, v in role_no.items()}
        for num in sorted(role_by_no, reverse=True):
            p2 = p2.replace(f"§{num}§", f"图{num}中的{role_by_no[num]}")
        p2 = p2.replace("图中的", "参考图中的")
        if not _re.match(r"^(参考图|图\d+中)", p2):
            p2 = "参考图中的角色，" + p2
        p2 += "。保持图一、图二等所有参考图中人物的形象、长相、发型、服装一致，不要改变人物。"
        return p2
    if p.startswith("图中的") or p.startswith("场景：") or p.startswith("参考图中的"):
        # v2 格式："场景：<基准>。图中只有…" → 把指代处前置"参考图中的"
        p = p.replace("。图中", "。参考图中", 1) if "。图中" in p else p
        if p.startswith("图中"):
            p = "参考图中的" + p[len("图中"):]
    elif p.startswith(("这个小男孩", "这个小女孩", "这个爸爸", "这个妈妈", "这一家人")):
        p = "参考图中的" + p
    elif "参考图" not in p:
        p = "参考图中的主角，" + p
    if "保持" not in p and "一致" not in p:
        p += "。保持参考图中人物的形象、长相、发型、服装一致，不要改变人物。"
    return p


def _toapis_upload(api_key: str, base: str, img_path: Path, cache: dict) -> str:
    """toapis 上传图片 → URL（带缓存，同文件不重复上传）。"""
    import uuid as _uuid

    key = str(img_path.resolve())
    if key in cache:
        return cache[key]
    boundary = "----b" + _uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{img_path.name}\"\r\nContent-Type: image/png\r\n\r\n".encode()
             + img_path.read_bytes() + b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"{base}/uploads/images", data=b"".join(parts), method="POST",
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    url = body.get("data", {}).get("url", "")
    if not url:
        raise RuntimeError(f"toapis 上传失败: {body}")
    cache[key] = url
    return url


def _toapis_gen(api_key: str, base: str, model: str, prompt: str,
                ref_urls: list[str], out_path: Path, w: int, h: int,
                timeout: float = 480.0) -> None:
    """toapis 异步生成：提交 → 轮询 → 下载保存。ref_urls 为空则纯文生图。"""
    body = {"model": model, "prompt": prompt, "n": 1, "size": "9:16",
            "resolution": "1k", "response_format": "url"}
    if ref_urls:
        body["image_urls"] = ref_urls
    req = urllib.request.Request(f"{base}/images/generations", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read().decode("utf-8"))
    task_id = resp.get("id") or resp.get("task_id")
    if not task_id:
        raise RuntimeError(f"toapis 提交失败: {resp}")
    print(f"  [img] toapis 任务已提交: {task_id[:24]}…")
    t0 = time.time()
    while True:
        time.sleep(8)
        req = urllib.request.Request(f"{base}/images/generations/{task_id}",
                                     headers={"Authorization": f"Bearer {api_key}"})
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
        st = r.get("status", "")
        if st == "completed":
            items = r.get("result", {}).get("data") or []
            if not items:
                raise RuntimeError(f"toapis 完成但无数据: {json.dumps(r, ensure_ascii=False)[:200]}")
            url = items[0].get("url", "")
            with urllib.request.urlopen(url, timeout=300) as fr:
                img = Image.open(fr).convert("RGB")
            _cover_resize(img, w, h).save(out_path, "PNG")
            print(f"  [img] toapis 完成（{time.time()-t0:.0f}s）→ {out_path.name}")
            return
        if st == "failed":
            raise RuntimeError(f"toapis 生成失败: {json.dumps(r, ensure_ascii=False)[:200]}")
        if time.time() - t0 > timeout:
            raise RuntimeError("toapis 生成超时")
        if int(time.time() - t0) % 30 < 2:
            print(f"  [img] toapis 生成中… {int(time.time()-t0)}s")


def _gen_api(scene: dict, cfg: dict, out_path: Path) -> None:
    """文生图 API。provider 优先顺序：toapis(gpt-image-2) > doubao(豆包) > siliconflow。

    toapis：上传参考图 → image_urls 异步生成（质量好，失败自动降级豆包）
    doubao：data URI 参考图，同步返回
    """
    import json as _json
    import urllib.request

    api_cfg = cfg["image"].get("api", {})
    key = api_cfg.get("api_key", "")
    if not key:
        sys.exit("[img] 未配置 API key：请到设置页「图片」tab 填写。")
    w = int(cfg["project"]["width"])
    h = int(cfg["project"]["height"])
    base = api_cfg.get("base_url", "https://api.siliconflow.cn/v1").rstrip("/")
    provider = api_cfg.get("provider", "siliconflow")
    model = api_cfg.get("model", "Kwai-Kolors/Kolors")
    size = api_cfg.get("size", "832x1216")
    # 参考图：按角色固定形象（cfg["image"]["refs"] = {角色: 图片路径}）
    import base64 as _b64

    refs = (cfg.get("image", {}).get("refs", {}) or {})
    role = scene.get("character") or scene.get("speaker", "旁白")
    ref_paths, ref_order = _collect_refs(scene, refs)
    if not ref_paths:
        rp = refs.get(role) or refs.get("全家")
        if rp and Path(rp).exists():
            ref_paths = [rp]
    has_ref = len(ref_paths) > 0

    if provider == "toapis":
        # toapis gpt-image-2：上传参考图 → 异步生成；失败降级 doubao
        try:
            cache = cfg["image"].setdefault("_toapis_upload_cache", {})
            ref_urls = [_toapis_upload(key, base, Path(p), cache) for p in ref_paths] if has_ref else []
            if has_ref:
                prompt = _ref_template(scene["image_prompt"], ref_paths, ref_order)
            else:
                prompt = scene["image_prompt"]
            _toapis_gen(key, base, model, prompt, ref_urls, out_path, w, h)
            return
        except Exception as e:
            print(f"  [img] ⚠️ toapis 失败（{e}），降级豆包重试…")
            provider = "doubao"
            fb = api_cfg.get("fallback") or {}
            if fb.get("api_key"):
                base = fb.get("base_url", base)
                key = fb["api_key"]
                model = fb.get("model", model)
                size = fb.get("size", size)
            else:
                print("  [img] 未配置豆包兜底（设置中心-图片-doubao），尝试用 toapis 参数直连豆包…")
    if provider == "doubao":
        if has_ref:
            # 豆包 API 必须显式写"参考图中的…保持…一致"才会启用参考图（网页端自动注入，API 不会）
            prompt = _ref_template(scene["image_prompt"], ref_paths, ref_order)
        else:
            prompt = scene["image_prompt"]
    elif has_ref:
        prompt = scene["image_prompt"] + "，参照参考图的画风与人物形象，保持风格和长相一致，高清，细节丰富"
    else:
        prompt = scene["image_prompt"] + "，高清，细节丰富，儿童绘本质感，五官清晰端正、圆润可爱、表情温和自然"

    if provider == "doubao":
        # 火山方舟：{model, prompt, size, response_format, image(参考图 data URI 或数组)}
        body = {"model": model, "prompt": prompt, "size": size, "response_format": "url",
                "watermark": False}
        if has_ref:
            imgs = [f"data:image/png;base64,{_b64.b64encode(Path(p).read_bytes()).decode()}" for p in ref_paths]
            body["image"] = imgs if len(imgs) > 1 else imgs[0]
    else:
        # 硅基流动：{model, prompt, image_size, batch_size, ..., image_url}
        body = {
            "model": model,
            "prompt": prompt,
            "image_size": size,
            "batch_size": 1,
            "num_inference_steps": 30,
            "guidance_scale": 7.5,
            "negative_prompt": "恐怖，诡异，畸形，白眼，恐怖的眼睛，五官扭曲，变形，低清晰度，噪点，水印",
        }
        if has_ref:
            body["image_url"] = f"data:image/png;base64,{_b64.b64encode(Path(ref_paths[0]).read_bytes()).decode()}"
    req = urllib.request.Request(
        f"{base}/images/generations",
        data=_json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    import time as _time

    resp = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                resp = _json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:  # 限频：等待后重试
                print(f"  [img] 429 限频，等待 {30 + attempt * 20}s 重试（{attempt+1}/5）…")
                _time.sleep(30 + attempt * 20)
                continue
            if e.code in (500, 503) and attempt < 5:
                print(f"  [img] {e.code} 服务繁忙，等待重试…")
                _time.sleep(20)
                continue
            if e.code == 400 and "negative" in str(e.read().decode("utf-8", errors="replace")):
                # 部分模型不支持 negative_prompt：去掉重试
                body.pop("negative_prompt", None)
                req = urllib.request.Request(
                    f"{base}/images/generations",
                    data=_json.dumps(body).encode("utf-8"),
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                continue
            raise
    if resp is None:
        raise RuntimeError("图片生成失败：多次重试仍失败")
    if provider == "doubao":
        # 火山方舟返回 OpenAI 格式：{"data":[{"url":...}]}
        items = resp.get("data") or []
        if not items:
            raise RuntimeError(f"图片生成失败：响应无 data（{_json.dumps(resp, ensure_ascii=False)[:300]}）")
        url = items[0].get("url") or items[0].get("b64_json") or ""
    else:
        items = resp.get("images") or []
        if not items:
            raise RuntimeError(f"图片生成失败：响应无 images（{_json.dumps(resp, ensure_ascii=False)[:300]}）")
        url = items[0].get("url") or ""
    if url.startswith("data:"):
        img = Image.open(io.BytesIO(_b64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    else:
        with urllib.request.urlopen(url, timeout=300) as r:
            img = Image.open(r).convert("RGB")
    _cover_resize(img, w, h).save(out_path, "PNG")


# ----------------------------------------------------------------- main ----
def _storyboard_hash(storyboard: dict, refs: dict | None = None) -> str:
    """分镜内容指纹：旁白+画面描述+参考图变化 → 缓存目录变化，避免图文不同步。"""
    import hashlib

    parts = []
    for sc in storyboard.get("scenes", []):
        parts.append(f"{sc.get('speaker','')}|{sc.get('character','')}|{sc.get('costume','')}|{sc.get('narration','')}|{sc.get('image_prompt','')}")
    parts.append(json.dumps(storyboard.get("scene_groups") or [], ensure_ascii=False))
    parts.append(json.dumps(refs or {}, ensure_ascii=False, sort_keys=True))
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()[:8]


def gen_scene_images(storyboard: dict, cfg: dict, out_dir: str | None = None) -> list[str]:
    if out_dir is None:
        refs = cfg.get("image", {}).get("refs", {}) or {}
        out_dir = f"output/images/{storyboard.get('story_id', 'default')}-{_storyboard_hash(storyboard, refs)}"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    backend = cfg["image"].get("backend", "placeholder")
    seed = int(cfg["image"].get("seed", 20260214))
    qa_enabled = str(cfg.get("image", {}).get("qa", "on")).lower() != "off"
    scenes = storyboard["scenes"]
    groups = storyboard.get("scene_groups") or list(range(len(scenes)))  # 无分组=一镜一图
    n_groups = max(groups) + 1 if groups else len(scenes)
    print(f"[M3 文生图] backend={backend}，{len(scenes)} 镜 → {n_groups} 张图（按组共图）"
          + ("，质检开" if qa_enabled else "，质检关"))
    paths = []
    for gid in range(n_groups):
        members = [sc for sc, g in zip(scenes, groups) if g == gid]
        if not members:
            continue
        sc = members[0]  # 组内第一镜的画面描述代表整组
        p = out / f"group_{gid:02d}.png"
        if p.exists():
            print(f"  [img] group {gid}: 命中缓存 {p.name}")
        else:
            rng = random.Random(seed + gid * 7919)
            if backend == "placeholder":
                _gen_placeholder(sc, cfg, p, rng)
            elif backend == "local":
                sys.exit("[img] backend=local 需要 NVIDIA GPU + diffusers，尚未接入。先用 placeholder 跑通，或见 README。")
            elif backend == "api":
                _gen_api(sc, cfg, p)
                import time
                time.sleep(float(cfg["image"].get("api", {}).get("sleep", 3.0)))  # 限频保护
                # 自动质检：角色不一致、或画面与旁白矛盾 → 自动重画（最多 3 次）。
                # 质检不可用（没配 key / 调用失败）时不再重画，直接报出原因。
                if qa_enabled:
                    try:
                        from . import qa
                        refs_map = (cfg.get("image", {}).get("refs", {}) or {})
                        rp, _ro = _collect_refs(sc, refs_map)
                        if not rp:
                            rp = [refs_map.get(sc.get("character") or sc.get("speaker", "旁白")) or refs_map.get("全家")]
                            rp = [x for x in rp if x and Path(x).exists()]
                        ok, n_try, reason = qa.check_image_strict(
                            rp, str(p), cfg, max_attempts=3,
                            gen_fn=lambda s, o: _gen_api(s, cfg, o), scene=sc,
                            narration=str(sc.get("narration", "")))
                        if ok and n_try > 1:
                            print(f"  [qa] group {gid}: 第 {n_try} 次重画后通过 ✓")
                        elif ok:
                            print(f"  [qa] group {gid}: 首次即通过 ✓")
                        else:
                            raise RuntimeError(f"配图 {gid + 1} 质检未通过：{reason}")
                    except Exception as e:
                        p.unlink(missing_ok=True)
                        raise RuntimeError(f"配图 {gid + 1} 质检失败：{e}") from e
            else:
                sys.exit(f"[img] 未知 backend: {backend}")
            print(f"  [img] group {gid}: {p.name}（{len(members)} 镜共用）")
        paths.append(str(p))
    return paths

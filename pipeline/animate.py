"""M4 动画模块：静态图 → 动态镜头（Ken Burns 推拉摇移 + 微动效）。

全部本地计算（OpenCV + numpy），零成本。
"""
from __future__ import annotations

import random
from pathlib import Path

import cv2
import numpy as np

MIN_SCENE_SECONDS = 3.0
PADDING_SECONDS = 0.3


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _scene_duration(audio: dict) -> float:
    if audio.get("visual_duration") is not None:
        return max(float(audio["visual_duration"]), 0.1)
    return max(audio["duration"] + PADDING_SECONDS, MIN_SCENE_SECONDS)


# ------------------------------------------------------------ effects ----
class Effects:
    """确定性微动效（同一 seed 结果一致）。"""

    def __init__(self, names: list[str], seed: int, w: int, h: int):
        self.w, self.h = w, h
        self.rng = random.Random(seed)
        self.stars = []
        if "twinkle_stars" in names:
            for _ in range(90):
                self.stars.append({
                    "x": self.rng.uniform(0, w),
                    "y": self.rng.uniform(0, h * 0.55),
                    "r": self.rng.uniform(0.6, 1.8),
                    "phase": self.rng.uniform(0, 6.28),
                    "freq": self.rng.uniform(0.5, 1.5),
                })
        self.fireflies = []
        if "fireflies" in names:
            for _ in range(9):
                self.fireflies.append({
                    "x": self.rng.uniform(0, w), "y": self.rng.uniform(h * 0.3, h * 0.85),
                    "vx": self.rng.uniform(-25, 25), "vy": self.rng.uniform(-15, 15),
                    "phase": self.rng.uniform(0, 6.28),
                    "freq": self.rng.uniform(1.0, 2.0),
                })
        self.clouds = []
        if "clouds_drift" in names:
            for _ in range(3):
                self.clouds.append({
                    "y": self.rng.uniform(h * 0.06, h * 0.35),
                    "scale": self.rng.uniform(0.6, 1.2),
                    "speed": self.rng.uniform(18, 40),
                    "x0": self.rng.uniform(0, w),
                })
        self.leaves = []
        if "falling_leaves" in names:
            for _ in range(14):
                self.leaves.append({
                    "x": self.rng.uniform(0, w), "y": self.rng.uniform(-h * 0.2, h),
                    "vy": self.rng.uniform(60, 120),
                    "sway": self.rng.uniform(20, 50),
                    "phase": self.rng.uniform(0, 6.28),
                    "freq": self.rng.uniform(0.8, 1.6),
                })

    def draw(self, frame: np.ndarray, t: float) -> np.ndarray:
        w, h = self.w, self.h
        if self.stars:
            for s in self.stars:
                a = 0.5 + 0.5 * np.sin(2 * np.pi * s["freq"] * t + s["phase"])
                b = int(255 * (0.25 + 0.75 * a))
                cv2.circle(frame, (int(s["x"]), int(s["y"])), int(max(1, s["r"])), (b, b, b), -1)
        if self.fireflies:
            for f in self.fireflies:
                x = (f["x"] + f["vx"] * t) % w
                y = (f["y"] + f["vy"] * t + 30 * np.sin(2 * np.pi * 0.3 * t + f["phase"])) % h
                a = 0.5 + 0.5 * np.sin(2 * np.pi * f["freq"] * t + f["phase"])
                glow = int(60 * a) + 30
                color = (int(120 + 80 * a), int(230 * a) + 25, 255)  # BGR 萤火虫黄绿
                cv2.circle(frame, (int(x), int(y)), 4, color, -1)
                cv2.circle(frame, (int(x), int(y)), 1, (255, 255, 255), -1)
        if self.clouds:
            for c in self.clouds:
                cx = (c["x0"] - c["speed"] * t) % (w + 400) - 200
                cy = c["y"]
                s = c["scale"]
                layer = np.zeros_like(frame)
                for dx, dy, rx, ry in ((0, 0, 90, 34), (40, -8, 60, 26), (-45, 6, 55, 24)):
                    cv2.ellipse(layer, (int(cx + dx * s), int(cy + dy * s)),
                                (int(rx * s), int(ry * s)), 0, 0, 360, (235, 235, 240), -1)
                frame = cv2.addWeighted(frame, 1.0, layer, 0.16, 0)
        if self.leaves:
            for lf in self.leaves:
                x = (lf["x"] + lf["sway"] * np.sin(2 * np.pi * lf["freq"] * t + lf["phase"])) % w
                y = (lf["y"] + lf["vy"] * t) % h
                cv2.circle(frame, (int(x), int(y)), 3, (60, 120, 200), -1)  # BGR 橙红落叶
        return frame


# ------------------------------------------------------------ camera ----
def _apply_camera(img: np.ndarray, cam: dict, t: float, out_w: int, out_h: int,
                  motion: str = "on") -> np.ndarray:
    """镜头运动；motion='none' 时图片完全静止（不做缩放/平移）。"""
    if motion == "none":
        return cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    h, w = img.shape[:2]
    move = cam.get("move", "zoom_in")
    s0 = float(cam.get("start_scale", 1.0))
    s1 = float(cam.get("end_scale", 1.12))
    st = _smoothstep(t)
    s = s0 + (s1 - s0) * st
    cw, ch = w / s, h / s
    dx = dy = 0.0
    pan_px = 0.05 * min(w, h)
    if move == "pan_left":
        dx = -pan_px * st
    elif move == "pan_right":
        dx = pan_px * st
    elif move == "pan_up":
        dy = -pan_px * st
    elif move == "pan_down":
        dy = pan_px * st
    cx = w / 2 + dx
    cy = h / 2 + dy
    x0 = int(max(0, min(cx - cw / 2, w - cw)))
    y0 = int(max(0, min(cy - ch / 2, h - ch)))
    crop = img[y0:int(y0 + ch), x0:int(x0 + cw)]
    return cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_LINEAR)


# ----------------------------------------------------------------- main ----
def make_scene_clips(storyboard: dict, audios: list[dict], cfg: dict,
                     images: list[str], out_dir: str = "output/clips",
                     title_card: str = "", title_seconds: float = 3.0) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    W = int(cfg["project"]["width"])
    H = int(cfg["project"]["height"])
    fps = int(cfg["project"].get("fps", 30))
    seed0 = int(cfg["image"].get("seed", 20260214))
    motion = str(cfg.get("animation", {}).get("motion", "on"))  # on=镜头运动, none=静止
    scenes = storyboard["scenes"]
    groups = storyboard.get("scene_groups") or list(range(len(scenes)))
    print(f"[M4 动画] {len(scenes)} 镜，{W}x{H}@{fps}fps，{len(images)} 张图")
    clips = []
    # 标题卡：视频首帧（标题大图，停留数秒）
    if title_card and Path(title_card).exists():
        tc = out / "title_card.mp4"
        img = cv2.imread(title_card)
        if img is not None:
            if img.shape[1] != W or img.shape[0] != H:
                img = cv2.resize(img, (W, H))
            writer = cv2.VideoWriter(str(tc), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
            n = max(2, int(round(title_seconds * fps)))
            for _ in range(n):
                writer.write(img)
            writer.release()
            clips.append(str(tc))
            print(f"  [anim] 标题卡: {title_seconds:.1f}s → title_card.mp4")
    for i, (sc, audio) in enumerate(zip(scenes, audios)):
        sid = sc["id"]
        dur = _scene_duration(audio)
        g = groups[i] if i < len(groups) else i
        # 同组镜共用同一张图：组图按整组累计时长做慢速推镜，避免快速切图
        if g < len(images):
            img = cv2.imread(images[g])
        else:
            img = cv2.imread(images[i]) if i < len(images) else None
        if img is None:
            raise SystemExit(f"[animate] 无法读取图片: group {g} ({images[g] if g < len(images) else '?'})")
        if img.shape[1] != W or img.shape[0] != H:
            img = cv2.resize(img, (W, H))
        cam = sc.get("camera", {})
        eff = Effects(sc.get("effects", []), seed0 + sid * 131, W, H)
        clip = out / f"scene_{sid:02d}.mp4"
        writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
        n = max(2, int(round(dur * fps)))
        # 组内第几镜：同一张图在多镜间做连续缓动，避免每镜都从头推镜
        g_idx = sum(1 for j in range(i) if groups[j] == g)
        g_total = sum(_scene_duration(a) for a, gg in zip(audios, groups) if gg == g)
        for f in range(n):
            t = f / (n - 1)
            # 组内偏移：从组开始累计推进
            t_group = (sum(_scene_duration(a) for a, gg in zip(audios[:i], groups[:i]) if gg == g) + t * dur) / max(g_total, 1e-6)
            frame = _apply_camera(img, cam, t, W, H, motion)
            frame = eff.draw(frame, t_group)
            writer.write(frame)
        writer.release()
        clips.append(str(clip))
        print(f"  [anim] scene {sid}: {dur:.1f}s (组{g}, 共{g_total:.1f}s) → {clip.name}")
    return clips

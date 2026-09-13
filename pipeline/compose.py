"""M6 合成模块：拼接镜头 + 旁白轨（含镜间停顿）+ BGM ducking + 烧字幕 → 成片。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import wave as _wave

from .animate import _scene_duration
from .config import ROOT

RATE = 44100


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def _read_wav(path: str | Path) -> np.ndarray:
    with _wave.open(str(path), "rb") as w:
        if w.getframerate() != RATE:
            raise SystemExit(f"[compose] 音频采样率不是 {RATE}: {path}")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if w.getnchannels() == 2:
        data = data[::2]  # 取左声道
    return data


def _write_wav(path: Path, data: np.ndarray) -> None:
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.astype(np.int16).tobytes())


def _concat_clips(clips: list[str], out_path: Path) -> None:
    list_file = out_path.with_suffix(".txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{Path(c).resolve().as_posix()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out_path)],
        check=True,
    )


def _normalize_audio(path: Path, eq: str = "") -> None:
    """响度标准化（loudnorm）+ 可选人声 EQ（清亮/饱满），保证旁白音量一致。"""
    tmp = path.with_suffix(".norm.wav")
    af = ["loudnorm=I=-16:TP=-1.5:LRA=11"]
    if eq:
        af.append(eq)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-af", ",".join(af), "-ar", str(RATE), "-ac", "1", str(tmp)],
        check=True,
    )
    tmp.replace(path)


# ------------------------------------------------------------ 音效 ----
def _sfx_cache_wav(name: str):
    """音效 mp3 → 44.1k 单声道 wav（缓存，统一压低响度避免刺耳）。"""
    src = ROOT / "assets" / "sfx" / f"{name}.mp3"
    if not src.exists():
        print(f"  [compose] 音效缺失: {src.name}，跳过该事件")
        return None
    cache = ROOT / "output" / "_tmp" / "sfx_cache"
    cache.mkdir(parents=True, exist_ok=True)
    wav = cache / f"{name}.wav"
    if not wav.exists():
        # 统一响度归一化到 -14 LUFS 并限制峰值 ≤ -2dB：音效清晰可闻（但 BGM/人声仍为主）
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-af", "loudnorm=I=-14:TP=-2:LRA=11",
             "-ar", str(RATE), "-ac", "1", str(wav)],
            check=True,
        )
    return wav


def _mix_sfx(storyboard: dict, audios: list[dict], narration_path: Path, out_path: Path,
             max_vol: float = 0.2, offset: float = 0.0, pause: float = 0.0,
             pauses: list[float] | None = None) -> None:
    """按分镜表 sfx 事件（{name, at, vol}）把音效混入旁白轨。vol 受全局上限约束。

    pause：统一的镜间静音间隔；pauses：逐镜自定义间隔（有则优先）。
    """
    data = _read_wav(narration_path).astype(np.float32) / 32768.0
    t = offset
    used = 0
    for idx, (sc, audio) in enumerate(zip(storyboard["scenes"], audios)):
        dur = float(audio.get("duration") or _scene_duration(audio))
        for ev in sc.get("sfx", []) or []:
            wav = _sfx_cache_wav(ev["name"])
            if wav is None:
                continue
            sfx = _read_wav(str(wav)).astype(np.float32) / 32768.0
            # vol 语义：音效相对旁白的响度比例。音效 loudnorm 到 -14 LUFS，
            # 旁白约 -16；乘 0.4~0.5 使音效清晰可闻但不抢人声。
            vol = min(float(ev.get("vol", 0.4)), max_vol)
            at = float(ev.get("at", 0))
            # at<0：距镜尾 X 秒处【开始】放音效（如 -0.1 = 镜尾前 0.1s 开始，
            # 音效延续进镜间停顿/收尾，作氛围烘托）。不再减去音效自身长度。
            if at < 0:
                at = max(0.0, dur + at)
            # at 的特殊值 9000+：表示"镜尾后 (at-9000) 秒"（如标题镜后停顿里放开头和弦）
            if at >= 9000:
                at = dur + (at - 9000)
            off = int((t + at) * RATE)
            end = min(off + len(sfx), len(data))
            if off < len(data):
                data[off:end] += sfx[: end - off] * vol
                used += 1
        gap = (pauses[idx] if pauses and idx < len(pauses) else pause)
        t += dur + gap
    print(f"  [compose] 混入音效 {used} 个事件（上限 {max_vol}）")
    _write_wav(out_path, (np.clip(data, -1, 1) * 32767).astype(np.int16))


def _resolve_bgm(storyboard: dict, comp_cfg: dict) -> tuple[str | None, float]:
    """配乐解析：分镜 bgm 支持 {track: 路径} 或 {mood: 情绪} 两种写法。

    mood 会从 assets/bgm/<mood>/ 目录自动选第一首。
    """
    sb_bgm = storyboard.get("bgm", {}) or {}
    bgm = sb_bgm.get("track") or ""
    if not bgm and sb_bgm.get("mood"):
        mood_dir = ROOT / "assets" / "bgm" / str(sb_bgm["mood"])
        if mood_dir.is_dir():
            files = sorted(mood_dir.glob("*.mp3"))
            if files:
                bgm = files[0].relative_to(ROOT).as_posix()
    if not bgm:
        bgm = comp_cfg.get("bgm") or ""
    vol = float(sb_bgm.get("volume") or comp_cfg.get("bgm_volume", 0.18))
    return (bgm or None), vol


def compose_video_with_approved_audio(clips: list[str], ass_path: str,
                                      audio_path: str, cfg: dict,
                                      out_path: str) -> str:
    """Compose images against the already approved final audio without re-TTS."""
    tmp = ROOT / "output" / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    concat_video = tmp / "concat_audio_first.mp4"
    _concat_clips(clips, concat_video)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out = (out if out.is_absolute() else ROOT / out).resolve()
    rel_ass = Path(ass_path).resolve().relative_to(ROOT).as_posix()
    rel_concat = concat_video.resolve().relative_to(ROOT).as_posix()
    audio = Path(audio_path).resolve().as_posix()
    bitrate = cfg["compose"].get("bitrate", "8M")
    abitrate = cfg["compose"].get("audio_bitrate", "192k")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", rel_concat, "-i", audio,
        "-vf", f"ass={rel_ass}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-maxrate", bitrate, "-bufsize", bitrate,
        "-c:a", "aac", "-b:a", abitrate,
        "-movflags", "+faststart", "-shortest", str(out),
    ]
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    return str(out)

"""M2.5 audio-first story synthesis.

The approved script is frozen before synthesis. Pauses, music, and the fixed
program ending sound come from the audio_story configuration.

用法：
  from pipeline.audio_story import synth_audio_story
  mp3 = synth_audio_story(sb, cfg, voices, out="output/audio_story.mp3")
"""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np

from .config import ROOT
from . import tts, compose

RATE = 44100
DEFAULT_SPEECH_RATE = 8
DEFAULT_PAUSE = 0.3


def synth_approved_audio_script(script_path: str, cfg: dict,
                                voices: dict | None = None,
                                out_path: str = "output/audio_story.mp3",
                                speech_rate: int = DEFAULT_SPEECH_RATE,
                                pause: float | None = None,
                                manifest_path: str = "") -> str:
    """Validate and synthesize a frozen canonical audio script.

    This is the production entry point for audio stories. It intentionally
    refuses draft, stale, or post-approval edited scripts before any paid TTS
    request can be made.
    """
    from .audio_script import AudioScriptError, assert_valid, to_storyboard

    path = Path(script_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        script = json.loads(path.read_text(encoding="utf-8"))
        report = assert_valid(script, require_approved=True)
    except (OSError, json.JSONDecodeError, AudioScriptError) as exc:
        raise AudioScriptError(f"拒绝合成未通过质量门的音频脚本: {exc}") from exc
    metrics = report["metrics"]
    print(f"[audio_story] 冻结脚本验证通过：旁白 {metrics['narrator_ratio']:.1%}，"
          f"台词 {metrics['dialogue_segments']} 段")
    storyboard = to_storyboard(script)
    return synth_audio_story(storyboard, cfg, voices=voices, out_path=out_path,
                             speech_rate=speech_rate, pause=pause,
                             manifest_path=manifest_path,
                             audio_script_digest=script["approval"]["digest"])


def _silence(sec: float) -> np.ndarray:
    return np.zeros(int(sec * RATE), dtype=np.int16)


def _read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def synth_audio_story(storyboard: dict, cfg: dict, voices: dict | None = None,
                      out_path: str = "output/audio_story.mp3",
                      speech_rate: int = DEFAULT_SPEECH_RATE,
                      pause: float | None = None,
                      manifest_path: str = "",
                      audio_script_digest: str = "") -> str:
    """合成完整音频故事 → mp3。返回输出路径。"""
    out = Path(out_path)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / f"_audio_story_{out.stem}"
    work.mkdir(parents=True, exist_ok=True)
    audio_cfg = cfg.get("audio_story") or {}
    segment_pause = float(audio_cfg.get("segment_pause", DEFAULT_PAUSE) if pause is None else pause)
    title_pause = float(audio_cfg.get("title_pause", 0.8))
    tail_silence = max(0.0, float(audio_cfg.get("tail_silence", 0.25)))
    # The established programme sign-off is the short four-note descent.
    ending_name = str(audio_cfg.get("ending_sound", "end_desc4") or "").strip()
    ending_volume = max(0.0, min(float(audio_cfg.get("ending_sound_volume", 0.45)),
                                  float(cfg.get("compose", {}).get("max_sfx_volume", 0.6))))
    ending_wav = compose._sfx_cache_wav(ending_name) if ending_name else None
    ending_audio = None
    if ending_wav:
        ending_audio = _read_wav(ending_wav).astype(np.float32) * ending_volume
        ending_audio = np.clip(ending_audio, -32768, 32767).astype(np.int16)
    ending_duration = len(ending_audio) / RATE if ending_audio is not None else 0.0

    # 1) 配音（语速注入到 doubao 角色）
    roles = dict(voices or {})
    for role, v in roles.items():
        if isinstance(v, dict) and v.get("engine") == "doubao":
            v = dict(v)
            v["speech_rate"] = speech_rate
            roles[role] = v
    results = tts.synth_scene_audios(storyboard, cfg, out_dir=str(work), voices_override=roles)

    # 2) Concatenate the frozen spoken segments with configured pauses.
    chunks = []
    timeline = []
    cursor = 0.0
    for i, r in enumerate(sorted(results, key=lambda x: x["scene_id"])):
        chunks.append(_read_wav(Path(r["wav"])))
        sc = storyboard["scenes"][i]
        gap = title_pause if sc.get("title") else segment_pause
        chunks.append(_silence(gap))
        timeline.append({
            "scene_id": sc["id"],
            "segment_id": sc.get("segment_id", ""),
            "speaker": sc.get("speaker", "旁白"),
            "text": sc.get("narration", ""),
            "title": bool(sc.get("title")),
            "start": round(cursor, 4),
            "duration": round(float(r["duration"]), 4),
            "visual_duration": round(float(r["duration"]) + gap, 4),
        })
        cursor += float(r["duration"]) + gap
    if tail_silence:
        chunks.append(_silence(tail_silence))
    if ending_audio is not None and len(ending_audio):
        chunks.append(ending_audio)
    if timeline:
        timeline[-1]["visual_duration"] = round(
            timeline[-1]["visual_duration"] + tail_silence + ending_duration, 4
        )
    data = np.concatenate(chunks)
    narration = work / "narration.wav"
    with wave.open(str(narration), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(data.tobytes())

    # 3) 旁白响度统一 → 混音效（氛围烘托）→ 限幅
    compose._normalize_audio(narration, cfg["compose"].get("voice_eq", ""))
    mixed = work / "mixed.wav"
    pauses = [title_pause if sc.get("title") else segment_pause for sc in storyboard["scenes"]]
    compose._mix_sfx(storyboard, results, narration, mixed,
                     float(cfg["compose"].get("max_sfx_volume", 0.6)),
                     offset=0.0, pauses=pauses)
    mixed.replace(narration)
    limit = work / "limit.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(narration),
                    "-af", "alimiter=limit=0.95", "-ar", str(RATE), "-ac", "1", str(limit)], check=True)
    limit.replace(narration)

    # 4) Resolve the series mood through the same generic BGM resolver as video.
    storyboard["bgm"] = {
        "mood": str(audio_cfg.get("bgm_mood") or "warm"),
        "volume": float(audio_cfg.get("bgm_volume", cfg.get("compose", {}).get("bgm_volume", 0.18))),
    }
    bgm, bgm_volume = compose._resolve_bgm(storyboard, cfg.get("compose") or {})
    bgm_path = Path(bgm) if bgm else None
    if bgm_path and not bgm_path.is_absolute():
        bgm_path = ROOT / bgm_path
    bgm_enabled = bool(audio_cfg.get("bgm_enabled", True))
    print(f"[audio_story] BGM: {bgm_path.name if bgm_path else 'off'} vol={bgm_volume}")
    if bgm_enabled and bgm_path and bgm_path.exists():
        total_s = len(data) / RATE
        title_dur = results[0]["duration"] if results else 0.0
        bgm_delay = title_dur + title_pause
        story_end = max(bgm_delay, total_s - ending_duration - tail_silence)
        fade_duration = max(0.2, float(audio_cfg.get("bgm_fade_out", 1.2)))
        fade_st = max(bgm_delay, story_end - fade_duration)
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", str(narration), "-stream_loop", "-1", "-i", str(bgm_path),
               "-filter_complex",
               f"[0:a]aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono,asplit=2[nar][sc];"
               f"[1:a]aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono,"
               f"volume={bgm_volume},"
               f"adelay={int(bgm_delay*1000)},afade=t=out:st={fade_st:.2f}:d={fade_duration:.2f}[bgm];"
               f"[bgm][sc]sidechaincompress=threshold=0.04:ratio=2.5:attack=120:release=500[bgmd];"
               f"[nar][bgmd]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,"
               f"alimiter=limit=0.92[aout]",
               "-map", "[aout]", "-ar", "44100", "-b:a", "192k", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(narration),
               "-ar", "44100", "-b:a", "192k", str(out)]
    subprocess.run(cmd, check=True)
    if manifest_path:
        manifest = Path(manifest_path)
        if not manifest.is_absolute():
            manifest = ROOT / manifest
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "schema_version": 1,
            "kind": "approved_audio_timeline",
            "title": storyboard.get("title", ""),
            "audio_file": str(out),
            "audio_script_digest": audio_script_digest,
            "total_duration": round(sum(row["visual_duration"] for row in timeline), 4),
            "scenes": timeline,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    # 清理工作目录（中间产物），只留成品
    import shutil
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    return str(out)

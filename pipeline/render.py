"""渲染入口：已确认音频 + 视觉方案 → 成片。

server.py 以子进程方式调用（python -m pipeline.render ...），日志写文件。
画面只复用已生成的配图，绝不重新配音、绝不在成片阶段临时生图。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import image_gen, animate, subtitles, compose
from .config import ROOT, load_config


def _load_storyboard(path: str) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def render_video_from_approved_audio(storyboard_path: str, cfg: dict,
                                     audio_file: str, audio_manifest: str,
                                     out: str = "", cover_template: str = "classic",
                                     cover_texts: dict | None = None,
                                     log=print) -> str:
    """Render visuals around the frozen audio instead of synthesizing speech again."""
    storyboard = _load_storyboard(storyboard_path)
    manifest = json.loads(Path(audio_manifest).read_text(encoding="utf-8"))
    approved_audio = Path(audio_file).resolve()
    manifest_audio = Path(manifest.get("audio_file", "")).resolve()
    if not approved_audio.is_file() or manifest_audio != approved_audio:
        raise RuntimeError("必须使用该时间轴记录的原始已确认 MP3")
    if manifest.get("audio_script_digest") != storyboard.get("audio_script_digest"):
        raise RuntimeError("音频时间轴与画面分镜不是同一份已批准音频脚本")
    from .visual_plan import validate_visual_plan
    validate_visual_plan(storyboard, manifest)

    # Visual beats and subtitle segments deliberately use different timelines.
    # One image may cover several narration/dialogue segments.
    visual_audios = [{
        "scene_id": scene["id"],
        "duration": float(scene["duration"]),
        "visual_duration": float(scene["duration"]),
    } for scene in storyboard.get("scenes", [])]
    subtitle_scenes = [{
        "id": row["scene_id"],
        "narration": row["text"],
        "keywords": [],
    } for row in storyboard.get("audio_segments", [])]
    subtitle_audios = [{
        "scene_id": row["scene_id"],
        "duration": float(row["duration"]),
        "visual_duration": float(row["visual_duration"]),
    } for row in storyboard.get("audio_segments", [])]
    manifest_ids = [str(row.get("segment_id") or "") for row in manifest.get("scenes", [])]
    plan_ids = [str(row.get("segment_id") or "") for row in storyboard.get("audio_segments", [])]
    if plan_ids != manifest_ids or len(subtitle_scenes) != len(subtitle_audios):
        raise RuntimeError("字幕时间轴与已确认音频不一致")

    images = [str(Path(path)) for path in storyboard.get("image_files") or []]
    if len(images) != len(storyboard.get("scenes", [])) or not all(Path(path).is_file() for path in images):
        raise RuntimeError("配图尚未全部生成，禁止在成片阶段临时重新生图")
    clips = animate.make_scene_clips(storyboard, visual_audios, cfg, images)
    ass = subtitles.generate_ass({"scenes": subtitle_scenes}, subtitle_audios, cfg, offset=0.0)
    final = compose.compose_video_with_approved_audio(
        clips, ass, audio_file, cfg, out or f"output/{storyboard.get('title', 'story')}.mp4"
    )
    log(f"==== 音频优先成片完成：{final} ====")
    return final


def main() -> None:
    ap = argparse.ArgumentParser(description="渲染成片（子进程入口）")
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--cover-template", default="classic")
    ap.add_argument("--cover-text", default="", help='封面文字覆盖，如 "badge=我的频道|subtitle=晚安故事"')
    ap.add_argument("--voices", default="{}", help='角色音色覆盖 JSON，如 {"牛牛":{"engine":"qwen","voice":"Ethan"}}')
    ap.add_argument("--approved-audio", default="", help="已批准并试听的最终音频文件")
    ap.add_argument("--audio-manifest", default="", help="最终音频对应的镜头时间轴 JSON")
    ap.add_argument("--log-file", default="", help="日志输出文件（服务器用，避免管道）")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    texts = {}
    for seg in args.cover_text.split("|"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            texts[k.strip()] = v.strip()

    def log(msg: str) -> None:
        print(msg)
        if args.log_file:
            with open(args.log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    # 模块内 print 也写入日志文件（服务器场景 stdout 被丢弃）
    import builtins
    import traceback

    _orig_print = builtins.print

    def _log_print(*a, **k):
        msg = " ".join(str(x) for x in a)
        _orig_print(*a, **k)
        if args.log_file:
            with open(args.log_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")

    builtins.print = _log_print
    try:
        if not (args.approved_audio and args.audio_manifest):
            raise RuntimeError("必须提供 --approved-audio 和 --audio-manifest"
                               "（画面只复用已确认 MP3，不在成片阶段重新配音）")
        render_video_from_approved_audio(
            args.storyboard, cfg, args.approved_audio, args.audio_manifest,
            args.out, args.cover_template, texts, log=log,
        )
    except BaseException:
        log("[render] 异常：\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

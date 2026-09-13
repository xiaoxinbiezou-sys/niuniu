"""数据存储（JSON 文件）：系列 / 故事 / 分镜 / 视频 / 任务 / 排期。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()


def _path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def _load(name: str, default: list | dict):
    p = _path(name)
    if not p.exists():
        return default
    try:
        with p.open(encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _save(name: str, data) -> None:
    p = _path(name)
    with p.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 设置 ----
DEFAULT_SETTINGS = {
    "llm": {
        "providers": {},          # {deepseek: {api_key}, qwen: {api_key}, doubao: {api_key}, glm: {api_key}, kimi: {api_key}}
        "story_model": "deepseek",
        "family_story_model": "",
        "imagination_story_model": "",
        # Legacy key retained for settings files created before the rename.
        "fantasy_story_model": "",
        "script_model": "deepseek",
    },
    "voice": {
        "providers": {"qwen": {"api_key": ""},
                      "doubao": {"x_api_key": "", "appid": "", "access_token": "", "resource_id": "seed-tts-2.0"}},
        "roles": {},              # {角色: {engine, voice/voice_type}}
        # 声音方案预设（组合1=原始混合引擎，组合2=全AI豆包+情绪）
        "active_preset": "preset2",
        "presets": {
            "preset1": {
                "name": "备用方案 · Edge + 千问",
                "roles": {
                    "旁白": {"engine": "edge", "edge_voice": "zh-CN-XiaoxiaoNeural"},
                    "爸爸": {"engine": "edge", "edge_voice": "zh-CN-YunjianNeural"},
                    "牛牛": {"engine": "qwen", "voice": "Mochi"},
                    "添添": {"engine": "qwen", "voice": "Stella"},
                    "妈妈": {"engine": "qwen", "voice": "Elias"},
                },
            },
            "preset2": {
                "name": "主方案 · 豆包语音 2.0 + 情绪",
                "roles": {
                    "旁白": {"engine": "doubao", "voice_type": "zh_female_xiaoxue_uranus_bigtts",
                             "narrate_instruct": "你用讲故事的亲切口吻，语气自然流畅，声音温暖"},
                    "爸爸": {"engine": "doubao", "voice_type": "zh_male_wennuanahu_uranus_bigtts"},
                    "牛牛": {"engine": "doubao", "voice_type": "zh_male_lanyinmianbao_uranus_bigtts",
                             "cry_instruct": "你是一个正在大哭的小男孩，声音带着哭腔和抽泣，边哭边说话，非常委屈和伤心"},
                    "添添": {"engine": "doubao", "voice_type": "ICL_uranus_zh_female_yuanqitianmei_tob"},
                    "妈妈": {"engine": "doubao", "voice_type": "zh_female_wenroumama_uranus_bigtts"},
                },
            },
        },
    },
    "image": {
        # 豆包为默认：toapis 长期连接超时，走它等于每次白等一轮再降级
        "provider": "doubao",
        "story_image_count": 6,   # 每个故事最多几张图（含封面节拍）：4 / 6 / 8 / 10
        "providers": {
            "siliconflow": {"api_key": "", "model": "Kwai-Kolors/Kolors"},
            "doubao": {"api_key": "", "model": "doubao-seedream-3-0-t2i-250415", "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
        },
        "cast": {},               # {角色: 形象描述}
        "refs": {"旁白": "全家福.png", "牛牛": "牛牛.png"},  # {角色: 参考图文件名}
    },
}


def get_settings() -> dict:
    s = _load("settings", {})
    # 补齐默认结构
    for k, v in DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = v
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                s[k].setdefault(k2, v2)
    return s


def save_settings(s: dict) -> dict:
    with _lock:
        merged = get_settings()
        _deep_merge(merged, s)
        _save("settings", merged)
        return merged


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------- 系列 ----
def list_series() -> list[dict]:
    return _load("series", [])


def get_series(sid: str) -> dict | None:
    for s in list_series():
        if s["id"] == sid:
            return s
    return None


def add_series(name: str, style: str = "watercolor", cover_template: str = "classic",
               bgm_mood: str = "warm", badge: str = "", subtitle: str = "",
               cast: dict | None = None, style_guide: str = "",
               refs: dict | None = None,
               series_bible: str = "") -> dict:
    with _lock:
        rows = list_series()
        s = {
            "id": _new_id(), "name": name, "style": style,
            "cover_template": cover_template, "bgm_mood": bgm_mood,
            "badge": badge or "儿童睡前故事", "subtitle": subtitle or "童话故事 · 温暖陪伴",
            "cast": cast or {}, "style_guide": style_guide,
            "refs": refs or {}, "series_bible": series_bible, "created": now(),
        }
        rows.append(s)
        _save("series", rows)
        return s


def update_series(sid: str, patch: dict) -> dict | None:
    with _lock:
        rows = list_series()
        for s in rows:
            if s["id"] == sid:
                s.update({k: v for k, v in patch.items() if v is not None})
                _save("series", rows)
                return s
    return None


# ---------------------------------------------------------------- 故事 ----
def list_stories() -> list[dict]:
    return _load("stories", [])


def get_story(sid: str) -> dict | None:
    for s in list_stories():
        if s["id"] == sid:
            return s
    return None


def add_story(series_id: str, idea: str, title: str, text: str,
              input_type: str = "auto", material_type: str = "complete",
              story_type_input: str = "auto", story_type: str = "family",
              story_facts: dict | None = None,
              story_digest: str = "", story_pipeline: str = "simple") -> dict:
    with _lock:
        rows = list_stories()
        s = {"id": _new_id(), "series_id": series_id, "idea": idea,
             "title": title, "text": text, "status": "draft",
             "input_type": input_type, "material_type": material_type,
             "story_type_input": story_type_input, "story_type": story_type,
             "story_facts": story_facts or {},
              "story_digest": story_digest,
              "story_pipeline": story_pipeline,
             "created": now(), "updated": now()}
        rows.insert(0, s)
        _save("stories", rows)
        return s


def update_story(sid: str, patch: dict) -> dict | None:
    with _lock:
        rows = list_stories()
        for s in rows:
            if s["id"] == sid:
                s.update({k: v for k, v in patch.items() if v is not None})
                s["updated"] = now()
                _save("stories", rows)
                return s
    return None


# ---------------------------------------------------------------- 分镜 ----
def list_storyboards() -> list[dict]:
    return _load("storyboards", [])


def get_storyboard(sid: str) -> dict | None:
    for s in list_storyboards():
        if s["id"] == sid:
            return s
    return None


def add_storyboard(story_id: str, data: dict, file: str = "") -> dict:
    with _lock:
        rows = list_storyboards()
        for r in rows:
            if r["story_id"] == story_id:
                r["data"] = data
                r["file"] = file
                _save("storyboards", rows)
                return r
        sb = {"id": _new_id(), "story_id": story_id, "data": data, "file": file, "created": now()}
        rows.append(sb)
        _save("storyboards", rows)
        return sb


def remove_storyboards(story_id: str) -> None:
    """Detach stale visual plans when their source story changes."""
    with _lock:
        current = list_storyboards()
        rows = [row for row in current if row.get("story_id") != story_id]
        if len(rows) != len(current):
            _save("storyboards", rows)


def remove_story(sid: str) -> None:
    with _lock:
        rows = [s for s in list_stories() if s["id"] != sid]
        _save("stories", rows)


# ---------------------------------------------------------------- 视频 ----
def list_videos() -> list[dict]:
    return _load("videos", [])


def get_video(vid: str) -> dict | None:
    for v in list_videos():
        if v["id"] == vid:
            return v
    return None


def add_video(story_id: str, storyboard_id: str, cover_template: str,
              cover_texts: dict, voices: dict | None = None) -> dict:
    with _lock:
        rows = list_videos()
        v = {"id": _new_id(), "story_id": story_id, "storyboard_id": storyboard_id,
             "status": "queued", "mp4": "", "cover": "", "cover_template": cover_template,
             "cover_texts": cover_texts or {}, "voices": voices or {},
             "scheduled_date": "", "created": now()}
        rows.insert(0, v)
        _save("videos", rows)
        return v


def update_video(vid: str, patch: dict) -> dict | None:
    with _lock:
        rows = list_videos()
        for v in rows:
            if v["id"] == vid:
                v.update({k: val for k, val in patch.items() if val is not None})
                _save("videos", rows)
                return v
    return None


def remove_video(vid: str) -> None:
    with _lock:
        rows = [v for v in list_videos() if v["id"] != vid]
        _save("videos", rows)


# ---------------------------------------------------------------- 任务 ----
def list_jobs() -> list[dict]:
    return _load("jobs", [])


def add_job(video_id: str) -> dict:
    with _lock:
        rows = list_jobs()
        j = {"id": _new_id(), "video_id": video_id, "status": "queued",
             "log": "", "created": now()}
        rows.insert(0, j)
        _save("jobs", rows)
        return j


def update_job(jid: str, patch: dict) -> dict | None:
    with _lock:
        rows = list_jobs()
        for j in rows:
            if j["id"] == jid:
                j.update({k: v for k, v in patch.items() if v is not None})
                _save("jobs", rows)
                return j
    return None


def get_job(jid: str) -> dict | None:
    for j in list_jobs():
        if j["id"] == jid:
            return j


def remove_job(jid: str) -> None:
    with _lock:
        rows = [j for j in list_jobs() if j["id"] != jid]
        _save("jobs", rows)


# ------------------------------------------------------------ 批量出片 ----
def list_batches() -> list[dict]:
    return _load("batches", [])


def get_batch(bid: str) -> dict | None:
    for b in list_batches():
        if b["id"] == bid:
            return b
    return None


def add_batch(series_id: str, ideas: list[str], image_count: int,
              story_type: str = "auto") -> dict:
    """一次批量出片任务。每个点子一条 item，带自己的阶段和状态。"""
    with _lock:
        rows = list_batches()
        b = {
            "id": _new_id(),
            "series_id": series_id,
            "status": "queued",
            "image_count": image_count,
            "story_type": story_type,
            "created": now(),
            "items": [
                {"index": i, "idea": idea, "status": "pending", "stage": "",
                 "stages": [], "story_id": "", "video_id": "", "title": "", "error": ""}
                for i, idea in enumerate(ideas)
            ],
        }
        rows.insert(0, b)
        _save("batches", rows)
        return b


def update_batch(bid: str, patch: dict) -> dict | None:
    with _lock:
        rows = list_batches()
        for b in rows:
            if b["id"] == bid:
                b.update({k: v for k, v in patch.items() if v is not None})
                _save("batches", rows)
                return b
    return None


def update_batch_item(bid: str, index: int, patch: dict) -> dict | None:
    with _lock:
        rows = list_batches()
        for b in rows:
            if b["id"] != bid:
                continue
            for item in b.get("items") or []:
                if item.get("index") == index:
                    item.update({k: v for k, v in patch.items() if v is not None})
                    _save("batches", rows)
                    return item
    return None


def append_batch_stage(bid: str, index: int, name: str, status: str,
                       detail: str = "") -> dict | None:
    """给某条批量记录追加一条阶段流水（哪个节点、什么时候、成没成）。

    前端据此显示"故事/脚本/音频/配图/成片"这些中间节点，失败时能直接看到断在哪一步。
    """
    with _lock:
        rows = list_batches()
        for b in rows:
            if b["id"] != bid:
                continue
            for item in b.get("items") or []:
                if item.get("index") != index:
                    continue
                stages = list(item.get("stages") or [])
                stages.append({"name": name, "status": status,
                               "detail": (detail or "")[:300], "at": now()})
                item["stages"] = stages[-20:]      # 只留最近 20 条
                _save("batches", rows)
                return item
    return None


def remove_batch(bid: str) -> None:
    with _lock:
        rows = [b for b in list_batches() if b["id"] != bid]
        _save("batches", rows)
    return None

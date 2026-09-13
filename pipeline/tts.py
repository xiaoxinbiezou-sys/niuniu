"""M2 配音模块：按分镜逐镜合成旁白音频，输出 wav + 每镜时长。

engine 支持：
  edge      —— edge-tts 免费在线音色（备用方案用）
  doubao    —— 火山引擎豆包语音（主方案，支持情绪指令）
  qwen      —— 阿里百炼千问 TTS
  gptsovits —— 本地 GPT-SoVITS 克隆音色（需自行启动推理服务）
  silence   —— 静音占位（无网络/无模型时测试流程）
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import wave
from pathlib import Path

import edge_tts

from .config import ROOT

RATE = 44100


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def _mp3_to_wav(mp3: Path, wav: Path) -> None:
    _ffmpeg("-i", str(mp3), "-ar", str(RATE), "-ac", "1", str(wav))


def _write_silence_wav(path: Path, seconds: float) -> None:
    import numpy as np
    frames = int(seconds * RATE)
    data = np.zeros(frames, dtype=np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


def _trim_tail_silence(path: Path, threshold_db: int = -45) -> None:
    """修剪句尾静音（避免镜间停顿割裂）。"""
    tmp = path.with_suffix(".trim.wav")
    _ffmpeg("-i", str(path), "-af",
            f"areverse,silenceremove=start_periods=1:start_threshold={threshold_db}dB:start_silence=0.05,areverse",
            str(tmp))
    tmp.replace(path)


# ------------------------------------------------------- 对话拆分 ----
_DIALOGUE_RE = re.compile(
    r"^(?P<prefix>.*?[说说道问喊叫回答])\s*[：:]\s*[“\"「『‘](?P<quote>.+?)[”\"」』’]\s*$", re.S
)


def _split_dialogue(narration: str):
    """识别 'XX说："……"' 模式 → (前缀, 引号台词)；无则 None。

    用于句内双音色：前缀用旁白声，引号内用角色声。
    """
    m = _DIALOGUE_RE.match(narration)
    if m:
        return m.group("prefix"), m.group("quote")
    return None


def _gpt_payload(text: str, vc: dict) -> dict:
    ref_audio = vc.get("ref_audio", "assets/voice_sample/ref_gpt.wav")
    ref_text = vc.get("ref_text", "") or None
    if not Path(ref_audio).is_absolute():
        ref_audio = str((ROOT / ref_audio).resolve())
    return {
        "text": text, "text_lang": "zh",
        "ref_audio_path": ref_audio,
        "prompt_text": ref_text or "",
        "prompt_lang": "zh", "media_type": "wav",
        "streaming_mode": False, "parallel_infer": False,
        "speed_factor": float(vc.get("speed", 1.0)),
        "text_split_method": vc.get("text_split_method", "cut0"),
        "batch_size": 1,
    }


def _gpt_post(vc: dict, payload: dict) -> bytes:
    import json as _json
    import urllib.request

    base = vc.get("api_url", "http://127.0.0.1:9880").rstrip("/")
    req = urllib.request.Request(
        f"{base}/tts",
        data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1800) as r:
        return r.read()


def _edge_synth(text: str, vc: dict, out_mp3: Path) -> None:
    """edge-tts 合成（带重试退避）；彻底失败时降级为克隆音色（gptsovits）。"""
    import time as _time

    voice = vc.get("edge_voice", "zh-CN-XiaoyiNeural")
    speed = float(vc.get("speed", 1.0))
    rate = f"{int(round((speed - 1.0) * 100)):+d}%"
    for attempt in range(5):
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out_mp3)))
            if out_mp3.exists() and out_mp3.stat().st_size > 500:
                return
        except Exception:
            pass
        _time.sleep(6 + attempt * 6)
    # edge 不可用（微软限流等）：降级用克隆音色，保证出片
    print(f"  [tts] ⚠️ edge-tts 不可用（{voice}），本句临时用克隆音色：{text[:16]}…")
    vc2 = dict(vc)
    vc2["engine"] = "gptsovits"
    tag = out_mp3.stem + "_fb"
    fb_wav = _synth_one(text, vc2, out_mp3.parent, tag)
    _ffmpeg("-i", str(fb_wav), "-b:a", "192k", str(out_mp3))
    fb_wav.unlink(missing_ok=True)


def _synth_one(text: str, vc: dict, out_dir: Path, tag: str) -> Path:
    """单句合成（edge/gptsovits/silence），统一 44.1k 单声道 + 尾部修剪。"""
    engine = vc.get("engine", "gptsovits")
    wav = out_dir / f"{tag}.wav"
    if engine == "edge":
        mp3 = wav.with_suffix(".mp3")
        _edge_synth(text, vc, mp3)
        _mp3_to_wav(mp3, wav)
        mp3.unlink(missing_ok=True)
    elif engine == "gptsovits":
        wav.write_bytes(_gpt_post(vc, _gpt_payload(text, vc)))
        tmp = wav.with_suffix(".44k.wav")
        _ffmpeg("-i", str(wav), "-ar", str(RATE), "-ac", "1", str(tmp))
        tmp.replace(wav)
    elif engine in ("doubao", "qwen"):
        tmp_dir = out_dir.parent / f"_s1_{tag}"
        tmp_dir.mkdir(exist_ok=True)
        fn = _synth_doubao if engine == "doubao" else _synth_qwen
        fn([{"id": 999, "narration": text}], vc, tmp_dir)
        src = tmp_dir / "scene_999.wav"
        src.replace(wav)
        tmp_dir.rmdir()
    elif engine == "silence":
        _write_silence_wav(wav, float(vc.get("silence_seconds", 3.0)))
    else:
        raise SystemExit(f"[tts] 未知 engine: {engine}")
    _trim_tail_silence(wav)
    return wav


def _concat_wavs(paths: list[Path], out: Path) -> None:
    """拼接多个 44.1k 单声道 wav。"""
    import numpy as np

    chunks = []
    for p in paths:
        with wave.open(str(p), "rb") as w:
            if w.getframerate() != RATE:
                raise SystemExit(f"[tts] 采样率不一致: {p}")
            chunks.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16))
    data = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


# ---------------------------------------------------------------- edge ----
def _synth_edge(scenes: list[dict], tts_cfg: dict, out_dir: Path) -> list[dict]:
    import time as _time

    results = []
    for sc in scenes:
        sid = sc["id"]
        mp3 = out_dir / f"scene_{sid:02d}.mp3"
        wav = out_dir / f"scene_{sid:02d}.wav"
        _edge_synth(sc["narration"], tts_cfg, mp3)
        _mp3_to_wav(mp3, wav)
        mp3.unlink(missing_ok=True)
        _trim_tail_silence(wav)
        results.append({"scene_id": sid, "wav": str(wav), "duration": _wav_duration(wav)})
        print(f"  [tts] scene {sid}: {sc['narration'][:20]}… {results[-1]['duration']:.2f}s")
        _time.sleep(0.6)  # 降低触发限流的频率
    return results


# ---------------------------------------------------------- gptsovits ----
def _synth_gptsovits(scenes: list[dict], tts_cfg: dict, out_dir: Path) -> list[dict]:
    """调用本地 GPT-SoVITS 推理服务（api_v2.py）做零样本克隆合成。

    服务启动方式（见 README）：
      cd models/GPT-SoVITS
      python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer_cpu.yaml
    """
    # 预热：请求一次服务，确认在线
    try:
        _gpt_post(tts_cfg, _gpt_payload("你好", tts_cfg))
    except Exception as e:
        sys.exit(f"[tts] GPT-SoVITS 服务不可用（{tts_cfg.get('api_url')}）：{e}\n请先启动推理服务，见 README。")

    results = []
    for sc in scenes:
        sid = sc["id"]
        wav = out_dir / f"scene_{sid:02d}.wav"
        # 短句/感叹句易被截断：时长不足时换随机种子重合成，取最长版本
        import random as _random
        min_expected = max(1.5, len(sc["narration"]) * 0.22)
        best_dur = 0.0
        best_bytes = None
        for attempt in range(4):
            payload = _gpt_payload(sc["narration"], tts_cfg)
            payload["seed"] = -1 if attempt == 0 else _random.randint(0, 2**31 - 1)
            wav.write_bytes(_gpt_post(tts_cfg, payload))
            tmp_wav = wav.with_suffix(".44k.wav")
            _ffmpeg("-i", str(wav), "-ar", str(RATE), "-ac", "1", str(tmp_wav))
            tmp_wav.replace(wav)
            _trim_tail_silence(wav)
            dur = _wav_duration(wav)
            if dur > best_dur:
                best_dur = dur
                best_bytes = wav.read_bytes()
            if dur >= min_expected:
                break
        if best_bytes is not None:
            wav.write_bytes(best_bytes)
        results.append({"scene_id": sid, "wav": str(wav), "duration": _wav_duration(wav)})
        print(f"  [tts] scene {sid}: {sc['narration'][:20]}… {results[-1]['duration']:.2f}s")
    return results


# ------------------------------------------------------------ silence ----
def _synth_silence(scenes: list[dict], tts_cfg: dict, out_dir: Path) -> list[dict]:
    seconds = float(tts_cfg.get("silence_seconds", 3.0))
    results = []
    for sc in scenes:
        sid = sc["id"]
        wav = out_dir / f"scene_{sid:02d}.wav"
        _write_silence_wav(wav, seconds)
        results.append({"scene_id": sid, "wav": str(wav), "duration": seconds})
        print(f"  [tts] scene {sid}: (静音占位 {seconds:.1f}s)")
    return results


# ------------------------------------------------------------ doubao ----# 火山引擎（豆包）语音合成。
# 两种端点：
#   V3 unidirectional（新版控制台 API Key）：
#     - X-Api-Key: <新版控制台 API Key>
#     - X-Api-Resource-Id: seed-tts-2.0（豆包语音合成模型 2.0 音色，uranus_bigtts 后缀）
#                          volc.service_type.10029（1.0 大模型音色，mars/moon_bigtts 后缀）
#     - req_params: {text, speaker, additions(JSON串), audio_params:{format,sample_rate}}
#     - emotion 放 audio_params.emotion / emotion_scale（仅部分音色支持，见音色列表-多情感）
#     - context_texts 放 additions（仅 TTS2.0 音色支持，语音指令控制情绪/哭腔/语速）
#     - 响应 = 多段 JSON 拼接，每段 data 字段是 base64 mp3 分块，需全部拼接
#   V1 经典 HTTP（旧版控制台 appid+token）：
#     - POST /api/v1/tts，app/token/cluster 鉴权，body 直接是 mp3 字节
_DOUBAO_V3_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_DOUBAO_V1_URL = "https://openspeech.bytedance.com/api/v1/tts"

# 情绪 → context_texts 语音指令（TTS2.0 音色，全AI方案用）
_DOUBAO_EMO_INSTRUCT = {
    "cry": "你是一个正在大哭的小男孩，声音带着哭腔和抽泣，边哭边说话，非常委屈和伤心",
    "sad": "你用非常悲伤、难过的语气说话，声音低沉",
    "scared": "你用害怕、颤抖的声音说话，声音发抖",
    "angry": "你用生气、气愤的语气说话",
    "excited": "你用兴奋、开心的语气说话，声音上扬",
    "surprise": "你用惊讶的语气说话",
    "anxious": "你用着急、有点担心的语气说话，语速稍快，像在等一个结果等不及了",
    "mystery": "你压低声音神秘地说，像在悄悄告诉对方一个秘密，带一点卖关子的感觉",
    "tender": "你用温柔、轻柔的语气说话",
    "happy": "你用开心、轻快的语气说话，声音带笑意",
    "proud": "你用骄傲、神气的语气说话，声音响亮",
    "brave": "你用勇敢、坚定的语气说话，声音有力",
    "whisper": "你用悄悄话的语气说话，声音轻而慢",
    "gentle": "你用温柔、耐心的语气说话，声音柔和",
    "question": "你用好奇、疑问的语气说话，尾音上扬",
    "sleepy": "你用困倦、迷糊的语气说话，声音慢而软",
    "laugh": "你用带着笑声的语气说话，开心又调皮",
    "triumph": "你用得意、炫耀的语气说话，声音上扬带劲",
    "narrate": "你用讲故事的亲切口吻，语气自然流畅",
    "awkward": "你有点不好意思地笑着说话，语气里带着尴尬和自嘲，像刚发现自己闹了个小笑话",
}

# 旁白讲故事情绪：按旁白内容关键词自动匹配讲故事的语气（全AI方案）
# 【克制原则】旁白是"讲故事的人"，语气有起伏但【不表演角色情绪】
# （哭/害怕是角色的情绪，由角色原声表达；旁白只平静叙述"谁哭了、怎么哭"）。
# 优先级从高到低，第一个命中的生效
_NARRATE_INSTRUCTS = [
    (("妖怪", "鬼", "害怕", "缩", "吓", "紧张"), "你讲故事讲到紧张处，语速稍慢、声音微微压低，但保持讲故事的平稳感"),
    (("哇", "欢呼", "跳起来", "开心", "笑", "第一名", "胜利", "得意"), "你讲故事讲到开心处，语气轻快带喜悦，声音上扬"),
    (("安静", "睡", "温柔", "轻轻", "慢慢", "月光", "星星"), "你用温柔、安静的讲故事语气，声音轻柔缓慢"),
    (("神秘", "悄悄", "奇怪", "咦", "偷偷"), "你讲故事讲到神秘处，声音压低带一点好奇"),
    (("跑", "追", "冲", "飞", "咚咚"), "你用轻快、节奏稍快的语气讲故事，声音有活力"),
    (("累", "疼", "难过", "叹气", "天黑", "哭", "眼泪"), "你平静地叙述孩子累了、哭了，语气温和带一点点心疼，但【不要模仿哭腔】，保持讲故事的口吻"),
]

# 旁白默认讲故事情绪（无关键词命中时）
_NARRATE_DEFAULT_INSTRUCT = "你用讲故事的亲切口吻，语气自然流畅，声音温暖"

# 情绪 → 1.0 emo_v2 音色的官方 emotion 参数值
_DOUBAO_EMO_PARAM = {
    "cry": "sad", "sad": "sad", "scared": "fear", "angry": "angry",
    "excited": "excited", "surprise": "surprised", "tender": "tender",
}


def _match_narrate_instruct(text: str, vc: dict) -> str:
    """旁白讲故事情绪：按旁白内容关键词匹配讲故事语气（全AI方案）。

    vc 可配 narrate_instruct 强制指定旁白语气（如系列级旁白风格）。
    """
    forced = vc.get("narrate_instruct")
    if forced:
        return forced
    for kws, instruct in _NARRATE_INSTRUCTS:
        if any(k in text for k in kws):
            return instruct
    return _NARRATE_DEFAULT_INSTRUCT


def _doubao_v3_synth(text: str, vc: dict, out_wav: Path) -> None:
    """豆包语音 V3 unidirectional 合成（新版控制台 API Key + seed-tts-2.0）。"""
    import json as _json
    import time as _time
    import urllib.request as _u

    api_key = vc.get("x_api_key", "") or vc.get("api_key", "")
    if not api_key:
        raise SystemExit("[tts] 未配置 doubao V3：config.yaml → tts.doubao.x_api_key（新版控制台 API Key）")
    speaker = vc.get("voice_type", "zh_male_shaonianzixin_uranus_bigtts")
    resource = vc.get("resource_id", "seed-tts-2.0")
    instruct = vc.get("instruct") or vc.get("context_texts")
    emotion = vc.get("emotion")
    emo_scale = int(vc.get("emotion_scale", 5))

    additions = {}
    if instruct:
        additions["context_texts"] = [instruct] if isinstance(instruct, str) else instruct
    audio_params = {"format": "mp3", "sample_rate": 24000}
    if emotion:
        audio_params["emotion"] = emotion
        audio_params["emotion_scale"] = emo_scale
    speech_rate = vc.get("speech_rate")
    if speech_rate:
        audio_params["speech_rate"] = int(speech_rate)

    body = {
        "req_params": {
            "text": text,
            "speaker": speaker,
            "additions": _json.dumps(additions, ensure_ascii=False) if additions else "",
            "audio_params": audio_params,
        }
    }
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "X-Api-Resource-Id": resource}

    def _decode_chunks(raw: bytes) -> bytes:
        """响应是多段 JSON 拼接，每段 data 是 base64 mp3 分块 → 拼接成完整 mp3。"""
        txt = raw.decode("utf-8", errors="replace")
        parts = []
        for m in re.finditer(r'"data":"([A-Za-z0-9+/=]+)"', txt):
            parts.append(m.group(1))
        if not parts:
            raise RuntimeError(f"doubao V3 无音频分块: {txt[:200]}")
        import base64
        return base64.b64decode("".join(parts))

    for attempt in range(4):
        try:
            req = _u.Request(_DOUBAO_V3_URL, data=_json.dumps(body).encode(),
                             headers=headers)
            with _u.urlopen(req, timeout=300) as r:
                raw = r.read()
            mp3 = _decode_chunks(raw)
            mp3_path = out_wav.with_suffix(".mp3")
            mp3_path.write_bytes(mp3)
            _mp3_to_wav(mp3_path, out_wav)
            mp3_path.unlink(missing_ok=True)
            return
        except Exception as e:
            if attempt == 3:
                raise
            print(f"  [tts] doubao V3 重试（{attempt+1}/4）: {e}")
            _time.sleep(5 * (attempt + 1))
    raise SystemExit("[tts] doubao V3 合成失败")


def _synth_doubao(scenes: list[dict], vc: dict, out_dir: Path) -> list[dict]:
    """火山引擎（豆包）语音合成。

    新版控制台 API Key（config.yaml → tts.doubao.api_key）→ 走 V3 unidirectional
    （seed-tts-2.0 音色，支持 context_texts 语音指令 / emotion 参数）；
    否则走 V1 经典端点（appid+token）。
    音色：vc["voice_type"]（如 zh_male_lanyinmianbao_uranus_bigtts 懒音绵宝 /
          zh_male_tiancaitongshen_uranus_bigtts 天才童声 / BV051_streaming 奶气萌娃）
    """
    import json as _json
    import time as _time
    import urllib.parse as _up
    import urllib.request as _u

    appid = vc.get("appid", "")
    token = vc.get("access_token", "")
    api_key = vc.get("x_api_key", "") or vc.get("api_key", "")
    use_v3 = bool(api_key)
    if not use_v3 and (not appid or not token):
        raise SystemExit("[tts] 未配置火山引擎 doubao：需要新版 API Key（tts.doubao.api_key）或 appid/access_token")
    cluster = vc.get("cluster", "volcano_tts")
    voice_type = vc.get("voice_type", "BV700_streaming")
    speed_ratio = float(vc.get("speed", 1.0))

    def _one(text: str, out_wav: Path, emo: str | None = None) -> None:
        if use_v3:
            vc3 = dict(vc)
            is_tts2 = str(vc.get("voice_type", "")).endswith("uranus_bigtts") \
                or vc.get("resource_id") == "seed-tts-2.0"
            if is_tts2:
                # 豆包语音 2.0：情绪走 context_texts 语音指令（emotion 参数不适用）
                # 角色专属指令（vc["instruct"]）优先：如爸爸"笑着温柔安慰"——不受通用情绪覆盖
                if vc.get("instruct"):
                    vc3["instruct"] = vc["instruct"]
                elif emo == "narrate":
                    # 旁白：按内容关键词匹配讲故事情绪
                    vc3["instruct"] = _match_narrate_instruct(text, vc)
                elif emo and emo in _DOUBAO_EMO_INSTRUCT:
                    vc3["instruct"] = vc.get("cry_instruct") \
                        if emo == "cry" and vc.get("cry_instruct") \
                        else _DOUBAO_EMO_INSTRUCT[emo]
            else:
                # 1.0 大模型音色（emo_v2 多情感）：emotion 参数
                if emo and emo in _DOUBAO_EMO_PARAM:
                    vc3["emotion"] = _DOUBAO_EMO_PARAM[emo]
            return _doubao_v3_synth(text, vc3, out_wav)

        params = {
            "app": {"appid": appid, "token": token, "cluster": cluster},
            "user": {"uid": "storystudio"},
            "audio": {"voice_type": voice_type, "encoding": "mp3",
                      "speed_ratio": speed_ratio, "volume_ratio": 1.0},
            "request": {"reqid": f"{int(_time.time()*1000)}", "text": text, "operation": "query"},
        }
        data = _up.urlencode({"params": _json.dumps(params)}).encode()
        req = _u.Request(_DOUBAO_V1_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        for attempt in range(4):
            try:
                with _u.urlopen(req, timeout=300) as r:
                    body = r.read()
                if body[:1] == b"{":
                    err = _json.loads(body.decode())
                    raise RuntimeError(f"doubao 错误: {err.get('message', body[:120])}")
                mp3 = out_wav.with_suffix(".mp3")
                mp3.write_bytes(body)
                _mp3_to_wav(mp3, out_wav)
                mp3.unlink(missing_ok=True)
                return
            except Exception as e:
                if attempt == 3:
                    raise
                print(f"  [tts] doubao 重试（{attempt+1}/4）: {e}")
                _time.sleep(5 * (attempt + 1))
        raise SystemExit("[tts] doubao 合成失败")

    results = []
    for sc in scenes:
        sid = sc["id"]
        wav = out_dir / f"scene_{sid:02d}.wav"
        _one(sc["narration"], wav, sc.get("emotion"))
        _trim_tail_silence(wav)
        results.append({"scene_id": sid, "wav": str(wav), "duration": _wav_duration(wav)})
        print(f"  [tts] scene {sid}: {sc['narration'][:20]}… {results[-1]['duration']:.2f}s"
              + (f" [情绪:{sc.get('emotion')}]" if sc.get("emotion") else ""))
    return results


# ------------------------------------------------------------ qwen ----
def _synth_qwen(scenes: list[dict], vc: dict, out_dir: Path) -> list[dict]:
    """阿里百炼（千问 Qwen3-TTS-Flash）非实时语音合成。

    配置：config.yaml → tts.qwen: {api_key, model}
    音色：vc["voice"]（如 Cherry / Maia / Mochi / Kai / Elias …）
    情绪：分镜 scene["emotion"] → 文本技巧（结巴/语气词）表达，不依赖指令
          （instruct-flash 的 instructions 会把多角色音色拉平，故用 flash + 文本技巧）
    """
    import json as _json
    import time as _time
    import urllib.request as _u

    api_key = vc.get("api_key", "")
    if not api_key:
        raise SystemExit("[tts] 未配置千问 qwen：config.yaml → tts.qwen.api_key")
    model = vc.get("model", "qwen3-tts-flash")
    voice = vc.get("voice", "Cherry")
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"

    def _emotion_text(text: str, emo: str) -> str:
        """文本级情绪表达（不改原意，只加口语化标记）：
        scared    —— 句首双字词结巴（"怪物！" → "怪…怪物！"）
        surprise  —— 句首加"哇/呀"，句尾感叹号保留
        excited   —— 句首加"哇塞/太棒了"语气词
        laugh     —— 句首加"哈哈，"笑声提示
        triumph   —— 句尾加"啦"收口，语气上扬
        awkward   —— 句首加"呃…"，表现不好意思
        whisper   —— 前后加"（小声）" 提示不适用于 TTS 文本，改用轻声词
        question  —— 句尾语气词"呀/呢"（若原句已是疑问句则保留）
        其余情绪 —— 文本原样（flash 靠文本自然朗读）
        """
        t = text.strip()
        if emo == "scared":
            m = re.match(r"^([\u4e00-\u9fa5])([\u4e00-\u9fa5])", t)
            if m:
                t = m.group(1) + "…" + m.group(1) + m.group(2) + t[2:]
            return t
        if emo == "awkward" and not t.startswith(("呃", "哎呀", "啊")):
            return "呃…" + t
        if emo == "mystery" and not t.startswith(("嘘", "悄悄")):
            return "嘘…" + t
        if emo == "anxious" and not t.endswith(("呀？", "吗？", "啦？", "呢？")):
            return t.rstrip("？?") + "呀？" if t.rstrip("？?") else t
        if emo == "laugh" and not t.startswith(("哈哈", "嘿", "噗")):
            return "哈哈，" + t
        if emo == "triumph" and not t.endswith(("啦！", "啦!", "了！", "了!")):
            return t.rstrip("！!") + "啦！" if t.rstrip("！!") else t
        if emo == "surprise" and not t.startswith(("哇", "呀", "咦")):
            return "哇，" + t if not t.startswith(("哇", "呀")) else t
        if emo == "excited" and not t.startswith(("哇", "太棒", "耶")):
            return "哇，" + t
        if emo == "question" and not t.endswith(("？", "?", "吗", "呢", "呀")):
            return t + "呀"
        return t

    def _one(text: str, out_wav: Path) -> None:
        body = {
            "model": model,
            "input": {"text": text},
            "parameters": {"voice": voice, "format": "wav", "sample_rate": 24000},
        }
        req = _u.Request(url, data=_json.dumps(body).encode(),
                         headers={"Authorization": f"Bearer {api_key}",
                                  "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with _u.urlopen(req, timeout=300) as r:
                    data = r.read()
                # qwen3-tts 返回 {output:{audio:{url}}}，需下载实际音频
                audio = data
                try:
                    j = _json.loads(data.decode())
                    if j.get("output", {}).get("audio", {}).get("url"):
                        with _u.urlopen(j["output"]["audio"]["url"], timeout=300) as ar:
                            audio = ar.read()
                    elif "message" in j:
                        raise RuntimeError(f"qwen 错误: {j['message']}")
                except (UnicodeDecodeError, _json.JSONDecodeError):
                    pass  # 直接音频字节
                if audio[:4] == b"RIFF":  # 已是 wav
                    out_wav.write_bytes(audio)
                else:  # mp3 等 → 转 wav
                    mp3 = out_wav.with_suffix(".mp3")
                    mp3.write_bytes(audio)
                    _mp3_to_wav(mp3, out_wav)
                    mp3.unlink(missing_ok=True)
                # 统一重采样为 44.1k 单声道（qwen 输出可能是 24k）
                _r = out_wav.with_suffix(".44k.wav")
                _ffmpeg("-i", str(out_wav), "-ar", str(RATE), "-ac", "1", str(_r))
                _r.replace(out_wav)
                return
            except Exception as e:
                if attempt == 3:
                    raise
                print(f"  [tts] qwen 重试（{attempt+1}/4）: {e}")
                _time.sleep(5 * (attempt + 1))
        raise SystemExit("[tts] qwen 合成失败")

    results = []
    for sc in scenes:
        sid = sc["id"]
        wav = out_dir / f"scene_{sid:02d}.wav"
        emo = (sc.get("emotion") or "").strip().lower()
        text = _emotion_text(sc["narration"], emo)
        _one(text, wav)
        _trim_tail_silence(wav)
        results.append({"scene_id": sid, "wav": str(wav), "duration": _wav_duration(wav)})
        print(f"  [tts] scene {sid}: {sc['narration'][:20]}… {results[-1]['duration']:.2f}s"
              + (f" [情绪:{emo}]" if emo else ""))
    return results


# ----------------------------------------------------------------- main ----
# 备用角色音色库：故事出现预设外的新角色时（奶奶/爷爷/小人/怪兽等），
# 按角色名关键词自动匹配音色。豆包语音2.0（seed-tts-2.0 资源）。
_RESERVE_VOICES = [
    # (关键词元组, 角色名, 音色, 指令)
    (("奶奶", "外婆", "姥姥"), "奶奶", "zh_female_popo_uranus_bigtts",
     "你是一位和蔼可亲的老奶奶，用慈祥、慢悠悠的语气说话"),
    (("爷爷", "外公", "姥爷"), "爷爷", "ICL_uranus_zh_male_huzishushu_tob",
     "你是一位慈祥的老爷爷，用温和、带笑意的语气说话"),
    (("小人", "精灵", "小矮人", "豆子"), "小人", "ICL_uranus_zh_female_jinglingxiangdao_tob",
     "你是一个小小的精灵/小人，声音细细的、轻快的"),
    (("国王", "王子", "公主"), "国王", "zh_male_baqiqingshu_uranus_bigtts",
     "你是国王/王室成员，用威严、沉稳的语气说话"),
    (("怪兽", "妖怪", "魔王", "怪物"), "怪兽", "zh_male_shenyeboke_uranus_bigtts",
     "你是怪兽/妖怪，用低沉、粗哑、有威慑力的声音说话"),
    (("老师", "老师"), "老师", "zh_female_yingyujiaoxue_uranus_bigtts",
     "你是老师，用温和、清晰、耐心的语气说话"),
    (("阿姨", "婶婶", "姑姑"), "阿姨", "zh_female_shuangkuaisisi_uranus_bigtts",
     "你是邻居阿姨，用亲切、爽朗的语气说话"),
    (("叔叔", "伯伯", "舅舅"), "叔叔", "zh_male_dongfanghaoran_uranus_bigtts",
     "你是叔叔，用爽朗、和气的语气说话"),
]


def _match_reserve_voice(role: str) -> dict | None:
    """按角色名关键词匹配备用音色库；未匹配返回 None（调用方兜底旁白声）。"""
    for kws, name, voice, instruct in _RESERVE_VOICES:
        if any(k in role for k in kws):
            return {"engine": "doubao", "voice_type": voice,
                    "instruct": instruct}
    return None


def synth_scene_audios(storyboard: dict, cfg: dict, out_dir: str = "output/audio",
                       voices_override: dict | None = None) -> list[dict]:
    """多角色配音：按分镜表的 speaker 字段分配音色，同音色镜头批量合成。

    音色来源优先级（高→低）：
      1. voices_override（前端自定义）：{角色: "clone" | "edge:voice_id"}
      2. config.yaml 的 voices 注册表（精确匹配 → 子串匹配）
      3. tts 段默认（当前为 GPT-SoVITS 添添音色）
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tts_cfg = cfg["tts"]
    voices = cfg.get("voices") or {}
    over = voices_override or {}
    scenes = storyboard["scenes"]

    def _apply_override(vc: dict, spec) -> dict:
        if spec == "clone":
            vc.update({"engine": "gptsovits"})
        elif isinstance(spec, str) and spec.startswith("edge:"):
            vc.update({"engine": "edge", "edge_voice": spec[5:]})
        elif isinstance(spec, dict):
            vc.update(spec)
        return vc

    def _resolve_vc(role: str) -> dict:
        vc = dict(tts_cfg)
        # 引擎专属配置（doubao/qwen 段）扁平化到顶层
        for prov in ("doubao", "qwen"):
            if prov in vc and isinstance(vc[prov], dict):
                vc.update({k: v for k, v in vc[prov].items() if v is not None})
        if role in over:
            return _apply_override(vc, over[role])
        role_cfg = voices.get(role) if isinstance(voices.get(role), dict) else None
        if role_cfg is None:
            for k, v in voices.items():
                if isinstance(v, dict) and k and k in role:
                    role_cfg = v
                    break
        if role_cfg is None:
            role_cfg = _match_reserve_voice(role)
        if role_cfg:
            vc.update(role_cfg)
        return vc

    narrator_vc = _resolve_vc("旁白")

    groups: dict[tuple, dict] = {}
    results: list[dict] = []
    for sc in scenes:
        role = sc.get("speaker", "旁白")
        vc = _resolve_vc(role)
        # 句内对话拆分："XX说：'台词'" → 前缀用旁白声，引号台词用角色声
        split = _split_dialogue(sc["narration"])
        if split:
            prefix, quote = split
            quote_vc = vc
            if role == "旁白":
                m = re.match(r"^(.*?)[说说道问喊叫回答]\s*[：:]?$", prefix)
                if m:
                    name = m.group(1).strip()
                    for k in list(over.keys()) + list(voices.keys()):
                        if k and (k == name or k in name):
                            quote_vc = _resolve_vc(k)
                            break
            wav_p = _synth_one(prefix, narrator_vc, out, f"scene_{sc['id']:02d}p")
            wav_q = _synth_one(quote, quote_vc, out, f"scene_{sc['id']:02d}q")
            wav = out / f"scene_{sc['id']:02d}.wav"
            _concat_wavs([wav_p, wav_q], wav)
            wav_p.unlink(missing_ok=True)
            wav_q.unlink(missing_ok=True)
            _trim_tail_silence(wav)
            dur = _wav_duration(wav)
            results.append({"scene_id": sc["id"], "wav": str(wav), "duration": dur})
            print(f"  [tts] scene {sc['id']}: (对话拆分) {prefix[:12]}…+{quote[:8]}… {dur:.2f}s")
            continue
        engine = vc.get("engine", "gptsovits")
        key = (engine, vc.get("edge_voice", ""), vc.get("voice", ""), vc.get("voice_type", ""),
               vc.get("ref_audio", ""), vc.get("ref_text", ""))
        groups.setdefault(key, {"cfg": vc, "scenes": []})["scenes"].append(sc)

    for key, g in groups.items():
        engine = key[0]
        print(f"[M2 配音] engine={engine}，{len(g['scenes'])} 镜（角色组）")
        if engine == "gptsovits":
            r = _synth_gptsovits(g["scenes"], g["cfg"], out)
        elif engine == "edge":
            r = _synth_edge(g["scenes"], g["cfg"], out)
        elif engine == "doubao":
            r = _synth_doubao(g["scenes"], g["cfg"], out)
        elif engine == "qwen":
            r = _synth_qwen(g["scenes"], g["cfg"], out)
        elif engine == "silence":
            r = _synth_silence(g["scenes"], g["cfg"], out)
        else:
            sys.exit(f"[tts] 未知 engine: {engine}（可选 edge/doubao/qwen/gptsovits/silence）")
        results += r
    results.sort(key=lambda x: x["scene_id"])
    return results

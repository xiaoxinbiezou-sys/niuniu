"""创作工作室后端：FastAPI + 任务队列 + 静态前端。

启动：
  D:\\new project\\.python\\python.exe -m studio.server --port 8000
打开：http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.config import load_config  # noqa: E402
from pipeline.audio_script import classify_errors, is_retryable as retryable_script_issues  # noqa: E402
from pipeline import cover as cover_mod  # noqa: E402
from pipeline import story_gen  # noqa: E402
from studio import store  # noqa: E402

PY = ROOT / ".python" / "python.exe"
STATIC = Path(__file__).resolve().parent / "static"
LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="故事创作工作室")


# ------------------------------------------------------------ 请求模型 ----
class SeriesIn(BaseModel):
    name: str
    style: str = "watercolor"
    cover_template: str = "classic"
    bgm_mood: str = "warm"
    badge: str = ""
    subtitle: str = ""
    cast: dict = {}       # 固定角色形象 {角色: 形象描述}
    style_guide: str = ""  # 系列写作风格指引（如佩奇式家庭日常）
    refs: dict = {}       # 固定角色参考图 {角色: 文件名}（存 assets/character_refs 下的文件名）
    series_bible: str = ""  # 系列故事内核文件（相对项目根目录）


class IdeaIn(BaseModel):
    idea: str
    series_id: str = ""
    material_type: str = "auto"
    story_type: str = "auto"


class ImportIn(BaseModel):
    title: str
    text: str
    series_id: str = ""
    material_type: str = "complete"
    story_type: str = "auto"


class EditIn(BaseModel):
    text: str
    material_type: str = ""
    story_type: str = ""
    series_id: str = ""


class FeedbackIn(BaseModel):
    feedback: str


class InstructionIn(BaseModel):
    instruction: str


class BatchIn(BaseModel):
    ideas: list[str]
    series_id: str = ""
    story_type: str = "auto"


class VideoIn(BaseModel):
    story_id: str
    cover_template: str = "classic"
    cover_texts: dict = {}


class ImageRegenerateIn(BaseModel):
    index: int


class StoryboardIn(BaseModel):
    max_images: int | None = None   # 4 / 6 / 8 / 10；留空用设置中心默认值


class ReviewIn(BaseModel):
    approve: bool


class ScheduleIn(BaseModel):
    date: str  # YYYY-MM-DD


# ------------------------------------------------------------ 工具 ----
LLM_PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "doubao": {"base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-1-6-250615"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "kimi": {"base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3"},
}


def llm_cfg(use: str = "story") -> dict:
    """从设置中心取写故事/分镜所用的大模型（provider + key）。"""
    s = store.get_settings()
    prov = s["llm"].get(f"{use}_model", "deepseek") or "deepseek"
    p = LLM_PROVIDERS.get(prov, LLM_PROVIDERS["deepseek"])
    key = (s["llm"]["providers"].get(prov, {}) or {}).get("api_key", "")
    return {"provider": prov, "base_url": p["base_url"], "model": p["model"],
            "api_key": key, "temperature": 0.9}


def story_llm_cfg(story_type: str) -> dict:
    settings = store.get_settings()["llm"]
    legacy = settings.get("story_model", "deepseek") or "deepseek"
    canonical_type = story_gen.normalize_story_type(story_type)
    field = "imagination_story_model" if canonical_type == "imagination" else "family_story_model"
    if canonical_type == "imagination":
        provider = settings.get(field) or settings.get("fantasy_story_model", "") or legacy
    else:
        provider = settings.get(field) or legacy
    key = (settings.get("providers", {}).get(provider, {}) or {}).get("api_key", "")
    if not key:
        provider = legacy
        key = (settings.get("providers", {}).get(provider, {}) or {}).get("api_key", "")
    model = LLM_PROVIDERS.get(provider, LLM_PROVIDERS["deepseek"])
    return {
        "provider": provider,
        "base_url": model["base_url"],
        "model": model["model"],
        "api_key": key,
        "temperature": 0.9,
    }


def voice_provider_cfg() -> dict:
    """从设置中心取 qwen/doubao 语音凭证，扁平化。"""
    s = store.get_settings()
    vp = s["voice"]["providers"]
    return {
        "qwen": dict(vp.get("qwen") or {}),
        "doubao": dict(vp.get("doubao") or {}),
    }


def image_provider_cfg() -> dict:
    """从设置中心取文生图凭证。"""
    s = store.get_settings()
    img = s["image"]
    provider = img.get("provider", DEFAULT_IMAGE_PROVIDER)
    if provider not in (img.get("providers") or {}):
        provider = DEFAULT_IMAGE_PROVIDER
    prov = (img.get("providers") or {}).get(provider) or {}
    return {
        "provider": provider,
        "base_url": prov.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
        "api_key": prov.get("api_key", ""),
        "model": prov.get("model", "doubao-seedream-4-0-250828"),
        "size": prov.get("size", "832x1216"),
    }


def effective_config(series_id: str = "") -> Path:
    """config.yaml + 设置中心凭证 → 合并后的渲染配置（render 子进程用）。

    series_id：若提供，角色参考图以【系列配置】优先，其次全局设置。
    """
    from pipeline.config import load_config

    cfg = load_config()
    vp = voice_provider_cfg()
    cfg["tts"]["qwen"]["api_key"] = vp["qwen"].get("api_key", "")
    cfg["tts"]["doubao"]["appid"] = vp["doubao"].get("appid", "")
    cfg["tts"]["doubao"]["access_token"] = vp["doubao"].get("access_token", "")
    # 新版 Key：设置中心优先，未填则保留 config.yaml 的值（避免覆盖为空）
    if vp["doubao"].get("x_api_key"):
        cfg["tts"]["doubao"]["x_api_key"] = vp["doubao"]["x_api_key"]
    if vp["doubao"].get("resource_id"):
        cfg["tts"]["doubao"]["resource_id"] = vp["doubao"]["resource_id"]
    ic = image_provider_cfg()
    cfg["image"]["backend"] = "api"
    cfg["image"]["api"]["provider"] = ic["provider"]
    cfg["image"]["api"]["base_url"] = ic["base_url"]
    cfg["image"]["api"]["api_key"] = ic["api_key"]
    cfg["image"]["api"]["model"] = ic["model"]
    cfg["image"]["api"]["size"] = ic["size"]
    # 备选平台（降级用）：豆包（方舟）作为 toapis 失败时的兜底
    img_cfg = store.get_settings()["image"]
    fallback = (img_cfg.get("providers") or {}).get("doubao") or {}
    cfg["image"]["api"]["fallback"] = {
        "provider": "doubao",
        "base_url": fallback.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
        "api_key": fallback.get("api_key", ""),
        "model": fallback.get("model", "doubao-seedream-4-0-250828"),
        "size": fallback.get("size", "832x1216"),
    }
    # 角色参考图（固定形象）：系列 refs 优先 → 全局设置兜底
    refs = {}
    ref_dir = ROOT / "assets" / "character_refs"
    series_refs = {}
    if series_id:
        series_refs = (store.get_series(series_id) or {}).get("refs") or {}
    merged = dict(store.get_settings()["image"].get("refs") or {})
    merged.update(series_refs)  # 系列优先
    for role, rel in merged.items():
        if rel:
            rel_p = Path(rel)
            if rel_p.is_absolute():
                p = rel_p
            elif rel_p.parent == Path(".") and (ref_dir / rel).exists():
                p = ref_dir / rel  # 设置页存的是文件名 → 参考图目录
            else:
                p = ROOT / rel
            refs[role] = str(p)
    cfg["image"]["refs"] = refs
    path = ROOT / "output" / "tmp" / "effective_config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml.dump(cfg, open(path, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    return path


def series_meta(series_id: str) -> dict:
    s = store.get_series(series_id) if series_id else None
    return s or {"id": "", "name": "默认", "style": "watercolor",
                 "cover_template": "classic", "bgm_mood": "warm",
                 "badge": "儿童睡前故事", "subtitle": "童话故事 · 温暖陪伴"}


# ------------------------------------------------------------ 系列 ----
@app.get("/api/templates")
def api_templates():
    return cover_mod.load_templates()


@app.get("/api/series")
def api_series_list():
    return store.list_series()


@app.post("/api/series")
def api_series_add(body: SeriesIn):
    return store.add_series(body.name, body.style, body.cover_template,
                            body.bgm_mood, body.badge, body.subtitle,
                            body.cast, body.style_guide, body.refs,
                            body.series_bible)


@app.put("/api/series/{sid}")
def api_series_update(sid: str, body: SeriesIn):
    s = store.update_series(sid, body.model_dump())
    if not s:
        raise HTTPException(404, "系列不存在")
    return s


# ------------------------------------------------------------ 故事 ----
def _ensure_story_type_fields(story: dict) -> dict:
    existing_type = story.get("story_type")
    existing_input = story.get("story_type_input")
    canonical_type = story_gen.STORY_TYPE_ALIASES.get(existing_type, existing_type)
    canonical_input = story_gen.STORY_TYPE_ALIASES.get(existing_input, existing_input)
    if canonical_type and canonical_input and canonical_type == existing_type and canonical_input == existing_input:
        return story
    meta = series_meta(story.get("series_id", ""))
    patch = {
        "story_type_input": canonical_input or "auto",
        "story_type": canonical_type or story_gen.detect_story_type(
            story.get("idea") or story.get("text", ""),
            style_guide=meta.get("style_guide", ""),
        ),
    }
    stored = store.update_story(story["id"], patch)
    if stored:
        return stored
    story.update(patch)
    return story


def _production_invalidated_patch() -> dict:
    return {
        "audio_script": "",
        "audio_status": "",
        "audio_url": "",
        "audio_file": "",
        "audio_manifest": "",
        "image_status": "",
        "visual_plan_digest": "",
    }


def _invalidate_story_production(story_id: str) -> None:
    store.remove_storyboards(story_id)


@app.post("/api/stories/generate")
def api_story_generate(body: IdeaIn):
    meta = series_meta(body.series_id)
    framework = meta.get("style_guide", "") or ""
    cast = meta.get("cast") or {}
    try:
        effective_type = story_gen.resolve_material_type(body.material_type, body.idea)
        effective_story_type = story_gen.resolve_story_type(
            body.story_type, body.idea, style_guide=framework
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        story_facts = story_gen.build_story_facts(
            None, body.idea, effective_type, effective_story_type, cast=cast
        )
        package = story_gen.generate_story_package(
            story_llm_cfg(effective_story_type), body.idea,
            cast=cast, style_guide=framework,
            series_bible=meta.get("series_bible", ""),
            material_type=effective_type,
            story_type=effective_story_type,
            facts=story_facts,
            enforce_quality=True,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return store.add_story(body.series_id, body.idea, package["title"], package["text"],
                           input_type=body.material_type, material_type=effective_type,
                           story_type_input=body.story_type,
                           story_type=effective_story_type,
                           story_facts=package["story_facts"],
                            story_digest=package["story_digest"],
                            story_pipeline="simple")


@app.post("/api/stories/import")
def api_story_import(body: ImportIn):
    """导入已有故事文本（不经过 AI 生成）。"""
    try:
        effective_type = story_gen.resolve_material_type(body.material_type, body.text)
        meta = series_meta(body.series_id)
        effective_story_type = story_gen.resolve_story_type(
            body.story_type, body.text, style_guide=meta.get("style_guide", "")
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return store.add_story(body.series_id, "", body.title, body.text,
                           input_type=body.material_type, material_type=effective_type,
                           story_type_input=body.story_type,
                           story_type=effective_story_type,
                           story_facts=story_gen.build_story_facts(
                               None, body.text, effective_type,
                               effective_story_type,
                               cast=meta.get("cast") or {},
                           ))


@app.post("/api/stories/batch")
def api_story_batch(body: BatchIn):
    meta = series_meta(body.series_id)
    cast = meta.get("cast") or {}
    style_guide = meta.get("style_guide", "") or ""
    out = []
    for i, idea in enumerate(body.ideas):
        idea = idea.strip()
        if not idea:
            continue
        effective_story_type = story_gen.resolve_story_type(
            body.story_type, idea, style_guide=style_guide
        )
        story_facts = story_gen.build_story_facts(
            None, idea, "idea", effective_story_type, cast=cast
        )
        package = story_gen.generate_story_package(
            story_llm_cfg(effective_story_type), idea, cast=cast, style_guide=style_guide,
            series_bible=meta.get("series_bible", ""),
            material_type="idea", story_type=effective_story_type,
            facts=story_facts,
        )
        out.append(store.add_story(body.series_id, idea, package["title"], package["text"],
                                   input_type="idea", material_type="idea",
                                   story_type_input=body.story_type,
                                   story_type=effective_story_type,
                                   story_facts=package["story_facts"],
                                   story_digest=package["story_digest"],
                                   story_pipeline="simple"))
        print(f"[batch] {i+1}/{len(body.ideas)} {package['title']}")
    return out


# ------------------------------------------------------------ 一键批量出片 ----
# 自动跑完：写故事 → 确认母稿 → 编译并批准脚本 → 合成音频 → 试听确认
#          → 视觉节拍 → 出图（含质检）→ 渲染成片
BATCH_STEPS = (
    "写故事", "确认母稿", "编译音频脚本", "批准脚本",
    "合成音频", "确认音频", "规划配图", "生成配图", "渲染成片",
)


def _batch_generate_story(item: dict, series_id: str, story_type: str) -> dict:
    """批量流程第 1 步：从点子在指定系列下写一篇故事。"""
    meta = series_meta(series_id)
    cast = meta.get("cast") or {}
    style_guide = meta.get("style_guide", "") or ""
    idea = item["idea"]
    effective_story_type = story_gen.resolve_story_type(
        story_type, idea, style_guide=style_guide)
    facts = story_gen.build_story_facts(None, idea, "idea", effective_story_type, cast=cast)
    package = story_gen.generate_story_package(
        story_llm_cfg(effective_story_type), idea, cast=cast, style_guide=style_guide,
        series_bible=meta.get("series_bible", ""),
        material_type="idea", story_type=effective_story_type,
        facts=facts, enforce_quality=True,
    )
    return store.add_story(series_id, idea, package["title"], package["text"],
                           input_type="idea", material_type="idea",
                           story_type_input=story_type,
                           story_type=effective_story_type,
                           story_facts=package["story_facts"],
                           story_digest=package["story_digest"],
                           story_pipeline="simple")


def _run_batch(batch: dict) -> None:
    """串行跑完整条出片流水线。每一步把阶段流水写回批量记录，前端据此显示过程节点。"""
    bid = batch["id"]
    series_id = batch.get("series_id", "")
    image_count = batch.get("image_count") or DEFAULT_IMAGE_COUNT
    story_type = batch.get("story_type") or "auto"
    meta = series_meta(series_id)
    cover_template = meta.get("cover_template") or "classic"

    store.update_batch(bid, {"status": "running"})
    for item in batch.get("items") or []:
        index = item["index"]
        story_id = ""
        try:
            def step(name: str, fn):
                """跑一个节点：开始/结束都记进流水，失败也记下来再抛。"""
                store.update_batch_item(bid, index, {"stage": name, "status": "running"})
                store.append_batch_stage(bid, index, name, "running")
                try:
                    result = fn()
                except Exception as exc:
                    store.append_batch_stage(bid, index, name, "failed", str(exc)[:300])
                    raise
                detail = ""
                if name == "写故事" and isinstance(result, dict):
                    detail = f"《{result.get('title','')}》"
                store.append_batch_stage(bid, index, name, "done", detail)
                return result

            story = step("写故事", lambda: _batch_generate_story(item, series_id, story_type))
            story_id = story["id"]
            store.update_batch_item(bid, index, {"story_id": story_id, "title": story["title"]})

            step("确认母稿", lambda: _confirm_story(story_id))

            # 编译 + 批准脚本必须一起重试：质量门实际是在"批准"这一步才报错的
            # （编译只负责生成，不校验），只包住编译等于重试永远不触发——实测踩过。
            script_error = ""

            def compile_and_approve():
                _generate_audio_script(story_id)
                _approve_audio_script(story_id)

            for fix_attempt in range(3):
                try:
                    step("编译并批准脚本", compile_and_approve)
                    script_error = ""
                    break
                except Exception as exc:
                    # 收 Exception 而不是 HTTPException：step() 会把异常原样重抛，
                    # 这里可能是 HTTPException，也可能是其它类型。
                    detail = getattr(exc, "detail", None)
                    issues = detail.get("issues") if isinstance(detail, dict) else None
                    if not (isinstance(issues, list) and issues):
                        issues = [detail if isinstance(detail, str) else str(exc)]
                    script_error = "；".join(str(x) for x in issues)
                    # 用错误**类别**判断能否靠改写母稿解决，而不是匹配零散文案
                    retryable = retryable_script_issues(issues)
                    print(f"[batch {bid}] 第 {index + 1} 条脚本不合规"
                          f"（类别={sorted(classify_errors(issues))} 可重试={retryable}）")
                    if fix_attempt >= 2 or not retryable:
                        raise
                    print(f"[batch {bid}] 第 {index + 1} 条：脚本不合规，重写故事再试"
                          f"（{fix_attempt + 1}/2）")
                    store.append_batch_stage(bid, index, "重写故事（脚本不合规）", "running",
                                             script_error[:120])
                    fresh = store.get_story(story_id) or {}
                    feedback = (
                        "上一版改编成音频脚本时未通过质量门，请针对性修改故事后重写：\n"
                        + script_error[:800]
                        + "\n要求：每句台词前必须由旁白用「角色名+说/问/喊/叫」引导"
                          "（例如：妈妈笑着说：“……”），不要出现没有引导的裸台词；"
                          "旁白要占大部分篇幅，连续两句台词中间必须插旁白。"
                    )
                    package = story_gen.generate_story_package(
                        story_llm_cfg(fresh.get("story_type", "family")),
                        item["idea"], original_text=fresh.get("text", ""),
                        feedback=feedback,
                        material_type="idea", story_type=fresh.get("story_type", "family"),
                        cast=meta.get("cast") or {},
                        style_guide=meta.get("style_guide", ""),
                        series_bible=meta.get("series_bible", ""),
                        facts=fresh.get("story_facts") or {},
                        enforce_quality=True,
                    )
                    store.update_story(story_id, {
                        "title": package["title"], "text": package["text"],
                        "story_digest": package["story_digest"],
                        "story_facts": package["story_facts"], "status": "draft",
                    })
                    _confirm_story(story_id)
                    store.append_batch_stage(bid, index, "重写故事（脚本不合规）", "done",
                                             f"《{package['title']}》")
            if script_error:
                raise HTTPException(400, script_error)

            step("合成音频", lambda: _render_story_audio(story_id))
            step("确认音频", lambda: _confirm_story_audio(story_id))
            step("规划配图", lambda: _build_storyboard(story_id, image_count))
            step("生成配图", lambda: _generate_images_for_story(story_id))

            result = step("渲染成片", lambda: _create_video(story_id, cover_template, {}))
            video_id = result["video"]["id"]
            store.update_batch_item(bid, index, {"video_id": video_id})

            # 等这条视频渲染完再进下一条：渲染本来也是串行队列
            for _ in range(240):
                v = store.get_video(video_id) or {}
                if v.get("status") in ("ready", "failed"):
                    break
                time.sleep(5)
            final = store.get_video(video_id) or {}
            if final.get("status") != "ready":
                raise RuntimeError(f"渲染未成功（状态 {final.get('status')}），见审核页任务日志")

            store.update_batch_item(bid, index, {"status": "done", "stage": "完成"})
            print(f"[batch {bid}] {index + 1} 完成：《{story['title']}》")
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else json.dumps(
                exc.detail, ensure_ascii=False)
            store.update_batch_item(bid, index, {"status": "failed", "error": detail})
            print(f"[batch {bid}] {index + 1} 失败（{item['stage'] or '写故事'}）：{detail}")
        except Exception as exc:  # 单条失败不能拖垮整批
            store.update_batch_item(bid, index, {"status": "failed", "error": str(exc)})
            print(f"[batch {bid}] {index + 1} 异常：{exc}")

    items = (store.get_batch(bid) or {}).get("items") or []
    failed = sum(1 for i in items if i.get("status") == "failed")
    store.update_batch(bid, {"status": "failed" if failed == len(items) else "done"})


def _batch_worker_loop() -> None:
    while True:
        try:
            pending = [b for b in store.list_batches() if b.get("status") == "queued"]
            if not pending:
                time.sleep(2)
                continue
            _run_batch(pending[-1])
        except Exception as exc:  # 线程不能因为一条批量任务死掉
            import traceback
            print(f"[batch] 批量任务异常：{exc}\n{traceback.format_exc()}")
            time.sleep(2)


class BatchIn2(BaseModel):
    """一键批量出片的请求体。"""
    ideas: list[str]
    series_id: str = ""
    image_count: int | None = None
    story_type: str = "auto"


@app.post("/api/batches")
def api_batch_create(body: BatchIn2):
    ideas = [i.strip() for i in body.ideas if i and i.strip()]
    if not ideas:
        raise HTTPException(400, "请先填点子，每行一个")
    if len(ideas) > 20:
        raise HTTPException(400, "一次最多 20 条（建议先跑 3~5 条试水）")
    if body.series_id and not store.get_series(body.series_id):
        raise HTTPException(400, "系列不存在")
    image_count = resolve_image_count(body.image_count)
    # 提前把明显的配置问题挡住，别等跑到一半才失败
    vset = store.get_settings()["voice"]
    preset = (vset.get("presets") or {}).get(vset.get("active_preset") or "preset2") or {}
    if not (preset.get("roles") or {}):
        raise HTTPException(400, "当前声音方案没有角色音色，请先到「设置 → 声音」配置")
    doubao_key = (vset.get("providers", {}).get("doubao") or {}).get("x_api_key")
    if not doubao_key and not load_config()["tts"]["doubao"].get("x_api_key"):
        raise HTTPException(400, "没有可用的配音凭证（设置 → 声音 → 豆包新版 API Key）")
    if not image_provider_cfg().get("api_key"):
        raise HTTPException(400, "没有可用的文生图凭证（设置 → 图片）")
    batch = store.add_batch(body.series_id, ideas, image_count, body.story_type)
    print(f"[batch] 新建批量任务 {batch['id']}，{len(ideas)} 条")
    return batch


@app.get("/api/batches")
def api_batch_list():
    return store.list_batches()


@app.get("/api/batches/{bid}")
def api_batch_get(bid: str):
    b = store.get_batch(bid)
    if not b:
        raise HTTPException(404, "批量任务不存在")
    return b


@app.delete("/api/batches/{bid}")
def api_batch_delete(bid: str):
    if not store.get_batch(bid):
        raise HTTPException(404, "批量任务不存在")
    store.remove_batch(bid)
    return {"ok": True}


@app.get("/api/stories")
def api_story_list():
    stories = [
        _ensure_story_type_fields(story)
        for story in store.list_stories()
    ]
    series_map = {s["id"]: s["name"] for s in store.list_series()}
    for st in stories:
        st.setdefault("input_type", "auto")
        st.setdefault("material_type", story_gen.detect_material_type(st.get("idea") or st.get("text", "")))
        st["series_name"] = series_map.get(st.get("series_id"), "")
        st["has_storyboard"] = any(sb["story_id"] == st["id"] for sb in store.list_storyboards())
    return stories


@app.get("/api/stories/{sid}")
def api_story_get(sid: str):
    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    s = _ensure_story_type_fields(s)
    # 故事库要显示"系列"和"输入类型"，这两个字段只在列表接口里补过，
    # 单条接口也得补，否则展开全文时系列会显示成空。
    s.setdefault("input_type", "auto")
    s.setdefault("material_type",
                 story_gen.detect_material_type(s.get("idea") or s.get("text", "")))
    series_map = {x["id"]: x["name"] for x in store.list_series()}
    s["series_name"] = series_map.get(s.get("series_id"), "")
    return s


def _remove_file(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _delete_video(vid: str) -> dict:
    """删掉一条成片记录及其文件（MP4 和封面）。不碰故事本身。"""
    video = store.get_video(vid)
    if not video:
        raise HTTPException(404, "视频不存在")
    removed = []
    mp4 = ROOT / (video.get("mp4") or "")
    if video.get("mp4") and mp4.is_file():
        _remove_file(mp4)
        removed.append(mp4.name)
    cover = ROOT / (video.get("cover") or "")
    if video.get("cover") and cover.is_file():
        _remove_file(cover)
        removed.append(cover.name)
    _remove_file(LOG_DIR / f"{vid}.log")
    store.remove_video(vid)
    # 这个视频对应的任务记录也一并清掉
    for job in store.list_jobs():
        if job.get("video_id") == vid:
            store.remove_job(job["id"])
    return {"ok": True, "removed": removed}


@app.delete("/api/videos/{vid}")
def api_video_delete(vid: str):
    return _delete_video(vid)


@app.delete("/api/stories/{sid}")
def api_story_delete(sid: str):
    """删掉故事，以及它名下的一切产物：脚本/音频/时间轴/配图/成片。"""
    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")

    removed_videos = []
    for v in [x for x in store.list_videos() if x["story_id"] == sid]:
        _delete_video(v["id"])
        removed_videos.append(v["id"])

    removed = []
    record = next((r for r in store.list_storyboards() if r["story_id"] == sid), None)
    if record:
        for path in (record.get("data") or {}).get("image_files") or []:
            p = Path(path)
            if p.is_file():
                _remove_file(p)
                removed.append(p.name)
        if record.get("file"):
            _remove_file(record["file"])
        store.remove_storyboards(sid)

    paths = _studio_audio_paths(sid)
    for key in ("story", "facts", "script", "manifest", "audio"):
        p = paths[key]
        if p.is_file():
            _remove_file(p)
            removed.append(p.name)
    # 图片目录整目录删掉（只属于这个故事的节拍图）
    for folder in (ROOT / "output" / "images").glob(f"studio_{sid}_*"):
        if folder.is_dir():
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)

    store.remove_story(sid)
    return {"ok": True, "removed": removed, "videos": removed_videos}


def _normalize_story_text(text: str) -> str:
    """Whitespace-insensitive form of a母稿, for deciding whether it really changed.

    The browser textarea hands back a trailing newline and may use CRLF, so a byte-for-byte
    comparison reports a "change" the author never made — which used to wipe an approved
    audio track and every generated image over a single stray space.
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _story_changed(body_text: str, stored_text: str) -> bool:
    return _normalize_story_text(body_text) != _normalize_story_text(stored_text)


@app.post("/api/stories/{sid}/edit")
def api_story_edit(sid: str, body: EditIn):
    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    patch = {"text": body.text, "status": "draft"}
    changed = _story_changed(body.text, s.get("text", ""))
    meta = series_meta(s.get("series_id", ""))
    if body.series_id and body.series_id != s.get("series_id", ""):
        target = store.get_series(body.series_id)
        if not target:
            raise HTTPException(400, "目标系列不存在")
        patch["series_id"] = body.series_id
        patch["story_facts"] = story_gen.build_story_facts(
            s.get("story_facts") or {},
            s.get("idea") or body.text,
            s.get("material_type", "complete"),
            s.get("story_type", "family"),
            cast=target.get("cast") or {},
        )
        meta = target
        changed = True
    if body.material_type:
        try:
            patch["input_type"] = body.material_type
            resolved = story_gen.resolve_material_type(
                body.material_type, s.get("idea") or body.text)
            patch["material_type"] = resolved
            changed = changed or resolved != s.get("material_type")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.story_type:
        try:
            patch["story_type_input"] = body.story_type
            resolved_type = story_gen.resolve_story_type(
                body.story_type,
                s.get("idea") or body.text,
                style_guide=meta.get("style_guide", ""),
            )
            patch["story_type"] = resolved_type
            patch["story_facts"] = story_gen.build_story_facts(
                None,
                s.get("idea") or body.text,
                patch.get("material_type", s.get("material_type", "complete")),
                resolved_type,
                cast=meta.get("cast") or {},
            )
            # Compare the *resolved* type: "auto" vs the concrete type it maps to is not a
            # content change, and the explicit selection is what the user saw.
            changed = changed or resolved_type != s.get("story_type")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if changed:
        # The母稿 really changed, so the frozen audio and every image are stale and must be
        # redone. Keep the digest in step with what is actually stored, otherwise the
        # confirm step would reject the very text we just saved.
        normalized = _normalize_story_text(body.text)
        patch["text"] = normalized
        patch["story_digest"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        patch.update(_production_invalidated_patch())
        _invalidate_story_production(sid)
    return store.update_story(sid, patch)


@app.post("/api/stories/{sid}/rewrite")
def api_story_rewrite(sid: str, body: FeedbackIn):
    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    meta = series_meta(s.get("series_id", ""))
    story_facts = story_gen.build_story_facts(
        s.get("story_facts") or {},
        s.get("idea") or s.get("text", ""),
        s.get("material_type", "complete"),
        s.get("story_type", "auto"),
        cast=meta.get("cast") or {},
    )
    try:
        package = story_gen.generate_story_package(
            story_llm_cfg(s.get("story_type", "family")),
            s.get("idea") or s["text"],
            original_text=s["text"], feedback=body.feedback,
            material_type=s.get("material_type", "complete"),
            story_type=s.get("story_type", "auto"),
            cast=meta.get("cast") or {},
            style_guide=meta.get("style_guide", ""),
            series_bible=meta.get("series_bible", ""),
            facts=story_facts,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    patch = {
        "title": package["title"],
        "text": package["text"],
        "status": "draft",
        "story_facts": package["story_facts"],
        "story_digest": package["story_digest"],
        "story_pipeline": "simple",
    }
    patch.update(_production_invalidated_patch())
    _invalidate_story_production(sid)
    return store.update_story(sid, patch)


@app.post("/api/stories/{sid}/modify")
def api_story_modify(sid: str, body: InstructionIn):
    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    meta = series_meta(s.get("series_id", ""))
    story_facts = story_gen.build_story_facts(
        s.get("story_facts") or {},
        s.get("idea") or s.get("text", ""),
        s.get("material_type", "complete"),
        s.get("story_type", "auto"),
        cast=meta.get("cast") or {},
    )
    try:
        package = story_gen.generate_story_package(
            story_llm_cfg(s.get("story_type", "family")),
            s.get("idea") or s["text"],
            original_text=s["text"],
            feedback="只修改用户指定内容，其余事件保持不变。" + body.instruction,
            material_type=s.get("material_type", "complete"),
            story_type=s.get("story_type", "auto"),
            cast=meta.get("cast") or {},
            style_guide=meta.get("style_guide", ""),
            series_bible=meta.get("series_bible", ""),
            facts=story_facts,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    patch = {
        "title": package["title"],
        "text": package["text"],
        "status": "draft",
        "story_facts": package["story_facts"],
        "story_digest": package["story_digest"],
        "story_pipeline": "simple",
    }
    patch.update(_production_invalidated_patch())
    _invalidate_story_production(sid)
    return store.update_story(sid, patch)


@app.post("/api/stories/{sid}/confirm")
def _confirm_story(sid: str) -> dict:
    """确认母稿（单条流程与批量流程共用）。"""
    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    s = _ensure_story_type_fields(s)
    meta = series_meta(s.get("series_id", ""))
    story_facts = story_gen.build_story_facts(
        s.get("story_facts") or {},
        s.get("idea") or s.get("text", ""),
        s.get("material_type", "complete"),
        s["story_type"],
        cast=meta.get("cast") or {},
    )
    digest = hashlib.sha256(s["text"].encode("utf-8")).hexdigest()
    if not s.get("text", "").strip():
        raise HTTPException(400, "故事正文不能为空")
    stored_digest = s.get("story_digest")
    if stored_digest and digest != stored_digest:
        raise HTTPException(400, "故事正文已被修改，请先保存修改")
    return store.update_story(sid, {
        "status": "confirmed",
        "story_facts": story_facts,
        "story_digest": digest,
        "story_pipeline": "simple",
    })


@app.post("/api/stories/{sid}/confirm")
def api_story_confirm(sid: str):
    return _confirm_story(sid)


# ------------------------------------------------------------ 分镜 ----
# How many stills a story may use, including the cover beat. Studio default is 6.
DEFAULT_IMAGE_COUNT = 6
IMAGE_COUNT_CHOICES = (4, 6, 8, 10)
# toapis 长期连接超时，默认走豆包，避免每次都先白等一轮再降级
DEFAULT_IMAGE_PROVIDER = "doubao"


def resolve_image_count(requested: int | None = None) -> int:
    """Storyboard size: explicit request → 设置中心默认 → built-in default."""
    if requested is not None:
        try:
            value = int(requested)
        except (TypeError, ValueError):
            raise HTTPException(400, "配图数量必须是数字")
        if value not in IMAGE_COUNT_CHOICES:
            raise HTTPException(400, f"配图数量只能是 {IMAGE_COUNT_CHOICES} 之一")
        return value
    configured = (store.get_settings().get("image") or {}).get("story_image_count")
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_IMAGE_COUNT
    return value if value in IMAGE_COUNT_CHOICES else DEFAULT_IMAGE_COUNT


def _build_storyboard(sid: str, max_images: int | None = None) -> dict:
    """规划视觉节拍（单条流程与批量流程共用）。"""
    from pipeline import visual_plan

    s = store.get_story(sid)
    if not s:
        raise HTTPException(404, "故事不存在")
    if s.get("audio_status") != "ready" or not s.get("audio_file") or not s.get("audio_manifest"):
        raise HTTPException(400, "必须先完成并试听正式音频，才能进入配图")
    image_count = resolve_image_count(max_images)
    audio_path = ROOT / s.get("audio_script", "")
    manifest_path = Path(s.get("audio_manifest", ""))
    frozen_audio = Path(s.get("audio_file", ""))
    if not audio_path.is_file():
        raise HTTPException(400, "正式音频脚本文件缺失")
    if not manifest_path.is_file() or not frozen_audio.is_file():
        raise HTTPException(400, "已确认音频或时间轴文件缺失")
    meta = series_meta(s.get("series_id"))
    cast = dict(meta.get("cast") or {})
    cast.update(store.get_settings()["image"].get("cast") or {})  # 设置中心的形象卡优先
    try:
        scene_llm = llm_cfg("script")
    except Exception:      # 设置里缺 llm 段时不该挡住规划画面
        scene_llm = None
    try:
        script = json.loads(audio_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        sb = visual_plan.build_visual_plan(
            script, manifest, facts=s.get("story_facts") or {}, cast=cast,
            max_images=image_count, llm_cfg=scene_llm,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    sb["style"] = meta["style"]
    sb["audio_file"] = s["audio_file"]
    sb["audio_manifest"] = s["audio_manifest"]
    sb["bgm"] = {"mood": meta["bgm_mood"]}
    sb["image_files"] = []
    sb["image_urls"] = []
    sb_file = ROOT / "stories" / f"storyboard.{sb['story_id']}.{store._new_id()}.json"
    sb_file.parent.mkdir(parents=True, exist_ok=True)
    sb_file.write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    rec = store.add_storyboard(sid, sb, str(sb_file))
    store.update_story(sid, {
        "image_status": "planned",
        "visual_plan_digest": sb["audio_script_digest"],
    })
    return rec


@app.post("/api/stories/{sid}/storyboard")
def api_storyboard_generate(sid: str, body: StoryboardIn | None = None):
    return _build_storyboard(sid, body.max_images if body else None)


@app.get("/api/stories/{sid}/storyboard")
def api_story_storyboard_get(sid: str):
    record = next((row for row in store.list_storyboards() if row.get("story_id") == sid), None)
    if not record:
        raise HTTPException(404, "尚未生成配图方案")
    return record


@app.get("/api/storyboards/{sid}")
def api_storyboard_get(sid: str):
    for r in store.list_storyboards():
        if r["id"] == sid:
            return r
    raise HTTPException(404, "配图方案不存在")


def _media_url(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to((ROOT / "output").resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"媒体文件不在 output 目录：{resolved}") from exc
    return f"/media/{rel}"


def _generate_images_for_story(sid: str) -> dict:
    """为故事已有的配图方案生成全部配图（批量流程用）。"""
    record = next((r for r in store.list_storyboards() if r["story_id"] == sid), None)
    if not record:
        raise HTTPException(400, "请先为该故事生成配图方案")
    return _generate_visual_images(record)


def _generate_visual_images(record: dict, force_index: int | None = None) -> dict:
    from pipeline import cover, image_director, image_gen

    story = store.get_story(record["story_id"])
    if not story or story.get("audio_status") != "ready":
        raise HTTPException(400, "必须先试听并确认正式音频")
    sb = dict(record.get("data") or {})
    if sb.get("audio_script_digest") != story.get("visual_plan_digest"):
        raise HTTPException(400, "配图方案已失效，请重新生成")
    cfg = load_config(str(effective_config(story.get("series_id", ""))))
    meta = series_meta(story.get("series_id", ""))
    cast = dict(meta.get("cast") or {})
    cast.update(store.get_settings()["image"].get("cast") or {})
    if not sb.get("image_directed"):
        # 一次调用为全部镜头写描述，并统一道具定义；失败就必须报出来，
        # 否则会用退化的描述静默出图（历史上前端永远看到"已导演"）
        if not image_director.apply_director(sb, llm_cfg("script"), cast):
            raise HTTPException(502, "图片策划（画面导演）失败：模型没有返回可用的画面描述，"
                                     "请检查「设置 → 模型 → 配图策划模型」的 Key 后重试")
        sb["image_directed"] = True
    image_dir = ROOT / "output" / "images" / f"studio_{story['id']}_{sb['audio_script_digest'][:8]}"
    if force_index is not None:
        if force_index < 0 or force_index >= len(sb.get("scenes") or []):
            raise HTTPException(400, "配图序号无效")
        stale = image_dir / f"group_{force_index:02d}.png"
        if stale.exists():
            stale.unlink()
    base_images = image_gen.gen_scene_images(sb, cfg, out_dir=str(image_dir))
    if len(base_images) != len(sb.get("scenes") or []):
        raise HTTPException(500, "配图数量与视觉节拍数量不一致")
    cover_path = ROOT / "output" / "covers" / f"story_{story['id']}_{sb['audio_script_digest'][:8]}.png"
    cover.generate_cover(
        sb, cfg, base_images[0], str(cover_path),
        sb.get("cover_template", "classic"), sb.get("cover_texts") or {},
    )
    images = list(base_images)
    images[0] = str(cover_path.resolve())
    sb["base_image_files"] = [str(Path(path).resolve()) for path in base_images]
    sb["base_image_urls"] = [_media_url(path) for path in base_images]
    sb["image_files"] = images
    sb["image_urls"] = [_media_url(path) for path in images]
    sb["cover_file"] = str(cover_path.resolve())
    Path(record["file"]).write_text(json.dumps(sb, ensure_ascii=False, indent=2), encoding="utf-8")
    updated = store.add_storyboard(record["story_id"], sb, record["file"])
    store.update_story(record["story_id"], {"image_status": "ready"})
    return updated


@app.post("/api/storyboards/{sid}/images")
def api_storyboard_images(sid: str):
    record = store.get_storyboard(sid)
    if not record:
        raise HTTPException(404, "配图方案不存在")
    return _generate_visual_images(record)


@app.post("/api/storyboards/{sid}/images/regenerate")
def api_storyboard_image_regenerate(sid: str, body: ImageRegenerateIn):
    record = store.get_storyboard(sid)
    if not record:
        raise HTTPException(404, "配图方案不存在")
    return _generate_visual_images(record, force_index=body.index)


# ------------------------------------------------------------ 设置中心 ----
@app.get("/api/settings")
def api_settings_get():
    return store.get_settings()


@app.put("/api/settings")
def api_settings_put(body: dict):
    return store.save_settings(body or {})


# 各引擎音色目录（设置页试听/选择用）
QWEN_VOICES = [
    ("Cherry", "芊悦 · 女·阳光"), ("Serena", "苏瑶 · 女·温柔"), ("Stella", "少女阿月 · 女童"),
    ("Bella", "萌宝 · 小萝莉·女童"), ("Bunny", "萌小姬 · 小萝莉·女童"), ("Nini", "邻家妹妹 · 女"),
    ("Momo", "茉兔 · 女·撒娇"), ("Chelsie", "千雪 · 女·二次元"), ("Vivian", "十三 · 女·小暴躁"),
    ("Ethan", "晨煦 · 男·阳光"), ("Ryan", "甜茶 · 男·戏感"), ("Mochi", "沙小弥 · 男童·小大人"),
    ("Pip", "顽屁小孩 · 男童"), ("Nofish", "不吃鱼 · 男"), ("Moon", "月白 · 男·率性"),
    ("Kai", "凯 · 男·低音"), ("Maia", "四月 · 女·知性"), ("墨讲师", "墨讲师 · 女·知性"),
    ("Bellona", "燕铮莺 · 女·洪亮"), ("Vincent", "田叔 · 男·烟嗓"), ("EldricSage", "沧明子 · 男·沉稳"),
    ("阿闻", "阿闻 · 男·播音"), ("小婉", "小婉 · 女·舒缓"), ("Katerina", "卡捷琳娜 · 女·御姐"),
    ("Elias", "Elias · 女·温柔"),
]
DOUBAO_VOICES = [
    ("BV700_streaming", "灿灿 · 女"), ("BV001_streaming", "通用女声"),
    ("BV002_streaming", "通用男声"), ("BV701_streaming", "擎苍 · 男"),
    ("BV051_streaming", "奶气萌娃 · 男童"), ("BV064_streaming", "小萝莉 · 女童"),
    ("BV421_streaming", "天才少女"), ("BV113_streaming", "甜宠少御"),
    # ---- 豆包语音 2.0（uranus_bigtts，支持语音指令/哭腔）----
    ("zh_male_lanyinmianbao_uranus_bigtts", "懒音绵宝 · 男童·软萌（2.0）"),
    ("zh_male_naiqimengwa_uranus_bigtts", "奶气萌娃 · 男童（2.0）"),
    ("zh_male_tiancaitongshen_uranus_bigtts", "天才童声 · 男童（2.0）"),
    ("zh_male_shaonianzixin_uranus_bigtts", "少年梓辛 · 少年（2.0）"),
    ("zh_male_linjiananhai_uranus_bigtts", "邻家男孩 · 少年（2.0）"),
    ("zh_female_xiaoxue_uranus_bigtts", "儿童绘本 · 女童（2.0）"),
    ("zh_female_shaoergushi_uranus_bigtts", "少儿故事 · 女童（2.0）"),
    ("zh_female_shuangkuaisisi_uranus_bigtts", "爽快思思 · 女（2.0）"),
    ("zh_male_ruyayichen_uranus_bigtts", "儒雅逸辰 · 男（2.0）"),
    ("zh_male_wennuanahu_uranus_bigtts", "温暖阿虎 · 男（2.0）"),
    ("zh_female_wenroumama_uranus_bigtts", "温柔妈妈 · 女（2.0）"),
    ("ICL_uranus_zh_female_yuanqitianmei_tob", "元气甜美 · 女童（豆包定制）"),
]


@app.get("/api/voice-catalog")
def api_voice_catalog():
    return {
        "qwen": QWEN_VOICES,
        "doubao": DOUBAO_VOICES,
        "edge": EDGE_VOICES,
    }


# ------------------------------------------------------------ 声音 ----
# 可用的系统音色（edge-tts 中文）+ 克隆音色
EDGE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女·温柔"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女·活泼"),
    ("zh-CN-XiaohanNeural", "晓涵 · 女·清新"),
    ("zh-CN-XiaomoNeural", "晓墨 · 女·知性"),
    ("zh-CN-XiaoxuanNeural", "晓萱 · 女·甜美"),
    ("zh-CN-XiaoruiNeural", "晓睿 · 女·童趣"),
    ("zh-CN-XiaoshuangNeural", "晓双 · 女·童声"),
    ("zh-CN-XiaoyouNeural", "晓悠 · 女·幼声"),
    ("zh-CN-XiaozhenNeural", "晓甄 · 女·温柔"),
    ("zh-CN-YunxiNeural", "云希 · 男·阳光"),
    ("zh-CN-YunjianNeural", "云健 · 男·青年"),
    ("zh-CN-YunyangNeural", "云扬 · 男·沉稳"),
    ("zh-CN-YunxiaNeural", "云夏 · 男·可爱"),
    ("zh-CN-YunhaoNeural", "云皓 · 男·少年"),
    ("zh-CN-YunfengNeural", "云枫 · 男·温柔"),
    ("zh-CN-YunyeNeural", "云野 · 男·开朗"),
    ("zh-CN-YunzeNeural", "云泽 · 男·少年"),
]


@app.get("/api/voices")
def api_voices():
    return [{"key": "clone", "name": "添添（克隆童声）", "type": "clone"}] + [
        {"key": f"edge:{vid}", "name": name, "type": "edge"} for vid, name in EDGE_VOICES
    ]


class PreviewIn(BaseModel):
    engine: str = "clone"   # clone | edge | qwen | doubao
    voice: str = ""         # qwen 音色 / edge voice
    voice_type: str = ""    # doubao 音色
    emotion: str = ""       # doubao 试听情绪：cry/sad/scared/angry/excited/surprise/tender
    text: str = "你好，我是今天的小主播，欢迎来听我讲故事！"


@app.post("/api/voices/preview")
def api_voice_preview(body: PreviewIn):
    import time as _time

    from pipeline import tts as tts_mod

    out_dir = ROOT / "output" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"pv_{_time.time_ns() % 10**9}"
    vp = voice_provider_cfg()

    vc = {}
    if body.engine == "clone":
        vc = {"engine": "gptsovits", **load_config()["tts"]}
    elif body.engine == "edge":
        vc = {"engine": "edge", "edge_voice": body.voice or "zh-CN-XiaoyiNeural", "speed": 1.0}
    elif body.engine == "qwen":
        vc = {"engine": "qwen", "voice": body.voice or "Cherry", "model": "qwen3-tts-flash", **vp["qwen"]}
    elif body.engine == "doubao":
        vc = {"engine": "doubao", "voice_type": body.voice_type or "BV700_streaming", **vp["doubao"]}
        # 设置中心未填豆包新版 Key 时，回退 config.yaml（避免覆盖为空）
        if not vc.get("x_api_key"):
            from pipeline.config import load_config
            dcfg = load_config()["tts"]["doubao"]
            vc.setdefault("x_api_key", dcfg.get("x_api_key", ""))
            vc.setdefault("resource_id", dcfg.get("resource_id", "seed-tts-2.0"))
        if body.emotion and body.emotion == "cry":
            vc["cry_instruct"] = tts_mod._DOUBAO_EMO_INSTRUCT["cry"]
    else:
        raise HTTPException(400, "未知引擎")
    wav = tts_mod._synth_one(body.text, vc, out_dir, tag)
    return {"url": f"/media/tmp/{wav.name}"}


REF_DIR = ROOT / "assets" / "character_refs"


@app.get("/api/refs")
def api_refs_list():
    """列出已确认的角色参考图（形象定妆照）。"""
    if not REF_DIR.is_dir():
        return []
    return [{"name": f.name, "url": f"/ref/{f.name}"}
            for f in sorted(REF_DIR.glob("*.png"))]


@app.get("/ref/{name}")
def ref_file(name: str):
    f = (REF_DIR / name).resolve()
    if f.exists() and f.is_file():
        return FileResponse(f)
    raise HTTPException(404, "参考图不存在")


class AudioStoryIn(BaseModel):
    audio_script: str = ""    # stories/audio.xxx.json，必须已批准并冻结
    voice_preset: str = ""   # preset1/preset2，空=当前 active
    speech_rate: int = 8
    out_name: str = ""       # 输出文件名（不含扩展名）


class StoryAudioRenderIn(BaseModel):
    speech_rate: int = 8


@app.post("/api/audio-story")
def api_audio_story(body: AudioStoryIn):
    """从已批准的正式音频脚本生成 mp3，禁止复用视频分镜。"""
    import time as _time

    from pipeline import audio_story
    from pipeline.config import load_config

    if not body.audio_script:
        raise HTTPException(400, "必须提供已批准的正式音频脚本")
    stories_root = (ROOT / "stories").resolve()
    script_path = (ROOT / body.audio_script).resolve()
    try:
        script_path.relative_to(stories_root)
    except ValueError:
        raise HTTPException(400, "音频脚本必须位于 stories 目录")
    if not script_path.is_file() or script_path.suffix.lower() != ".json":
        raise HTTPException(404, "音频脚本不存在")

    # 配置：凭证 + 声音方案
    cfg = load_config()
    vset = store.get_settings()["voice"]
    presets = vset.get("presets") or {}
    pid = body.voice_preset or vset.get("active_preset") or "preset1"
    preset = presets.get(pid) or {}
    roles = dict(preset.get("roles") or {})
    cfg["tts"]["doubao"]["x_api_key"] = vset["providers"]["doubao"].get("x_api_key") or \
        cfg["tts"]["doubao"].get("x_api_key", "")
    cfg["tts"]["doubao"]["resource_id"] = vset["providers"]["doubao"].get("resource_id") or \
        "seed-tts-2.0"
    cfg["tts"]["qwen"]["api_key"] = vset["providers"]["qwen"].get("api_key") or \
        cfg["tts"]["qwen"].get("api_key", "")

    requested_name = Path(body.out_name).stem if body.out_name else f"audio_{_time.time_ns() % 10**9}"
    safe_name = "".join(ch for ch in requested_name if ch.isalnum() or ch in "-_") or "audio_story"
    out = ROOT / "output" / "audio_story" / f"{safe_name}.mp3"
    try:
        path = audio_story.synth_approved_audio_script(
            str(script_path), cfg, voices=roles,
            out_path=str(out), speech_rate=body.speech_rate,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": f"/media/audio_story/{Path(path).name}", "file": str(path)}


def _studio_audio_paths(story_id: str) -> dict[str, Path]:
    base = ROOT / "stories" / "studio"
    base.mkdir(parents=True, exist_ok=True)
    return {
        "story": base / f"story.{story_id}.md",
        "facts": base / f"story.{story_id}.facts.json",
        "script": base / f"audio.{story_id}.json",
        "manifest": ROOT / "output" / "audio_story" / f"story_{story_id}.manifest.json",
        "audio": ROOT / "output" / "audio_story" / f"story_{story_id}.mp3",
    }


def _relative_root(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")


@app.post("/api/stories/{sid}/audio-script")
def _generate_audio_script(sid: str, annotate: bool = True) -> dict:
    """从已确认母稿编译音频脚本（单条流程与批量流程共用）。

    ``annotate=True`` 时先让模型标注"哪句是谁说的 / 哪些对白改成旁白"。
    这是脚本层该干的活：故事层只要求好看，说话人和旁白占比由这里解决。
    标注失败会自动回退到语法推断，不会让流程崩掉。
    """
    from pipeline import audio_script

    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")
    if story.get("status") != "confirmed":
        raise HTTPException(400, "必须先确认故事母稿")
    paths = _studio_audio_paths(sid)
    facts = story_gen.build_story_facts(
        dict(story.get("story_facts") or {}), story.get("idea") or story["text"],
        story.get("material_type", "complete"), story.get("story_type", "family"),
        cast=series_meta(story.get("series_id", "")).get("cast") or {},
    ) or {}
    paths["story"].write_text(f"# {story['title']}\n\n{story['text']}\n", encoding="utf-8")
    paths["facts"].write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if annotate:
            # 标注是"理解"任务，不需要创造力：低温更稳、更快
            cfg = dict(llm_cfg("script"))
            cfg["temperature"] = 0.3
            script = audio_script.compile_with_annotation(
                story["title"], story["text"], facts,
                _relative_root(paths["story"]), _relative_root(paths["facts"]),
                llm_cfg=cfg,
            )
        else:
            script = audio_script.compile_audio_script_text(
                story["title"], story["text"], facts,
                _relative_root(paths["story"]), _relative_root(paths["facts"]),
            )
        report = audio_script.validate_audio_script(script, story_body=story["text"], facts=facts)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    paths["script"].write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    stored = store.update_story(sid, {
        "audio_script": _relative_root(paths["script"]),
        "audio_status": "draft",
        "audio_url": "",
        "audio_file": "",
        "audio_manifest": "",
        "story_facts": facts,
        "image_status": "",
        "visual_plan_digest": "",
    })
    _invalidate_story_production(sid)
    return {"story": stored, "script": script, "report": report,
            "file": _relative_root(paths["script"])}


@app.post("/api/stories/{sid}/audio-script")
def api_story_audio_script_generate(sid: str):
    return _generate_audio_script(sid)


@app.get("/api/stories/{sid}/audio-script")
def api_story_audio_script_get(sid: str):
    from pipeline import audio_script

    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")
    rel = story.get("audio_script", "")
    path = ROOT / rel if rel else None
    if not path or not path.is_file():
        raise HTTPException(404, "尚未生成音频脚本")
    script = json.loads(path.read_text(encoding="utf-8"))
    report = audio_script.validate_audio_script(
        script, story_body=story["text"], facts=story.get("story_facts") or {},
        require_approved=script.get("status") == "approved",
    )
    return {"story": story, "script": script, "report": report, "file": rel}


def _approve_audio_script(sid: str) -> dict:
    """批准并冻结音频脚本（单条流程与批量流程共用）。"""
    from pipeline import audio_script

    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")
    path = ROOT / story.get("audio_script", "")
    if not path.is_file():
        raise HTTPException(404, "尚未生成音频脚本")
    try:
        script = audio_script.approve_script(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    stored = store.update_story(sid, {"audio_status": "approved"})
    return {"story": stored, "script": script, "report": script["qa"],
            "file": story["audio_script"]}


@app.post("/api/stories/{sid}/audio-script/approve")
def api_story_audio_script_approve(sid: str):
    return _approve_audio_script(sid)


def _render_story_audio(sid: str, speech_rate: int = 8) -> dict:
    """合成正式 MP3 + 时间轴（单条流程与批量流程共用）。"""
    from pipeline import audio_story

    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")
    if story.get("audio_status") not in ("approved", "rendered", "ready"):
        raise HTTPException(400, "必须先审核并批准音频脚本")
    paths = _studio_audio_paths(sid)
    vset = store.get_settings()["voice"]
    preset = (vset.get("presets") or {}).get(vset.get("active_preset") or "preset2") or {}
    cfg = load_config()
    cfg.setdefault("audio_story", {})["bgm_mood"] = series_meta(
        story.get("series_id", "")
    ).get("bgm_mood", "warm")
    cfg["tts"]["doubao"]["x_api_key"] = vset["providers"]["doubao"].get("x_api_key") or cfg["tts"]["doubao"].get("x_api_key", "")
    cfg["tts"]["doubao"]["resource_id"] = vset["providers"]["doubao"].get("resource_id") or "seed-tts-2.0"
    cfg["tts"]["qwen"]["api_key"] = vset["providers"]["qwen"].get("api_key") or cfg["tts"]["qwen"].get("api_key", "")
    try:
        output = audio_story.synth_approved_audio_script(
            str(paths["script"]), cfg, voices=dict(preset.get("roles") or {}),
            out_path=str(paths["audio"]), speech_rate=speech_rate,
            manifest_path=str(paths["manifest"]),
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    stored = store.update_story(sid, {
        "audio_status": "rendered",
        "audio_url": f"/media/audio_story/{Path(output).name}",
        "audio_file": str(Path(output).resolve()),
        "audio_manifest": str(paths["manifest"].resolve()),
        "image_status": "",
        "visual_plan_digest": "",
    })
    _invalidate_story_production(sid)
    return {"story": stored, "url": stored["audio_url"],
            "file": stored["audio_file"], "manifest": stored["audio_manifest"]}


@app.post("/api/stories/{sid}/audio")
def api_story_audio_render(sid: str, body: StoryAudioRenderIn):
    return _render_story_audio(sid, body.speech_rate)


def _confirm_story_audio(sid: str) -> dict:
    """试听确认（单条流程与批量流程共用）。批量流程里等同自动确认。"""
    from pipeline import audio_script

    story = store.get_story(sid)
    if not story:
        raise HTTPException(404, "故事不存在")
    if story.get("audio_status") not in ("rendered", "ready"):
        raise HTTPException(400, "请先合成正式音频")
    audio = Path(story.get("audio_file", ""))
    manifest_path = Path(story.get("audio_manifest", ""))
    script_path = ROOT / story.get("audio_script", "")
    if not audio.is_file() or not manifest_path.is_file() or not script_path.is_file():
        raise HTTPException(400, "正式音频、脚本或时间轴文件缺失")
    script = json.loads(script_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = (script.get("approval") or {}).get("digest", "")
    if not digest or manifest.get("audio_script_digest") != digest:
        raise HTTPException(400, "音频时间轴与已批准脚本不一致")
    report = audio_script.validate_audio_script(
        script, story_body=story.get("text", ""), facts=story.get("story_facts") or {},
        require_approved=True,
    )
    if not report["ok"]:
        raise HTTPException(400, {"message": "音频脚本未通过当前质量门", "issues": report["errors"]})
    stored = store.update_story(sid, {"audio_status": "ready"})
    return {"story": stored, "url": stored["audio_url"]}


@app.post("/api/stories/{sid}/audio/confirm")
def api_story_audio_confirm(sid: str):
    return _confirm_story_audio(sid)


class ImagePreviewIn(BaseModel):
    prompt: str
    character: str = ""   # 角色名（生成定妆照时提示用）
    ref: str = ""         # 参考图文件名（可选）


@app.post("/api/images/preview")
def api_image_preview(body: ImagePreviewIn):
    import time as _time

    from pipeline import image_gen

    out_dir = ROOT / "output" / "tmp"
    out_dir.mkdir(parents=True, exist_ok=True)
    ic = image_provider_cfg()
    cfg = {
        "project": {"width": 1080, "height": 1920},
        "image": {"backend": "api", "api": ic, "seed": int(_time.time() % 100000), "refs": {}},
    }
    if body.ref:
        cfg["image"]["refs"]["全家"] = str(REF_DIR / body.ref)
    scene = {"id": 1, "image_prompt": body.prompt, "speaker": "全家"}
    out = out_dir / f"ip_{_time.time_ns() % 10**9}.png"
    image_gen._gen_api(scene, cfg, out)
    return {"url": f"/media/tmp/{out.name}", "file": str(out)}


# ------------------------------------------------------------ 视频/任务 ----
def _enqueue(video: dict) -> dict:
    job = store.add_job(video["id"])
    return job


def _recover_interrupted_jobs() -> int:
    """把上次进程留下的"制作中"任务标成中断。

    队列是串行的，worker 一次只处理一个 job。进程被关掉、或渲染子进程被杀掉时，
    job 会永远停在 running：重启后没人认领，而队列又以为有任务在进行，于是后面
    排队的任务全部卡死（实测有任务卡了一天多）。
    """
    recovered = 0
    for job in store.list_jobs():
        if job.get("status") != "running":
            continue
        store.update_job(job["id"], {
            "status": "failed",
            "log": (job.get("log") or "") + "\n[中断] 服务重启，该任务没有跑完，请重新提交",
        })
        video = store.get_video(job["video_id"])
        if video and video.get("status") == "rendering":
            store.update_video(video["id"], {"status": "failed"})
        recovered += 1
    if recovered:
        print(f"[startup] 已把 {recovered} 个中断的渲染任务标记为失败")
    return recovered


def _worker_loop():
    """串行渲染队列（子进程，日志写文件避免管道限制）。"""
    _recover_interrupted_jobs()
    while True:
        try:
            jobs = [j for j in store.list_jobs() if j["status"] == "queued"]
            if not jobs:
                time.sleep(2)
                continue
            job = jobs[0]
            _run_job(job)
        except Exception as exc:  # 任何异常都不能让整条队列线程静默死掉
            import traceback
            print(f"[worker] 任务处理异常：{exc}\n{traceback.format_exc()}")
            try:
                if "job" in dir() and job.get("id"):
                    store.update_job(job["id"], {"status": "failed", "log": f"内部错误：{exc}"})
            except Exception:
                pass
            time.sleep(2)


def _run_job(job: dict) -> None:
    video = store.get_video(job["video_id"])
    if not video:
        store.update_job(job["id"], {"status": "failed", "log": "video 不存在"})
        return
    store.update_job(job["id"], {"status": "running"})
    store.update_video(video["id"], {"status": "rendering"})

    sb = None
    for r in store.list_storyboards():
        if r["id"] == video["storyboard_id"]:
            sb = r
            break
    if not sb or not sb.get("file") or not Path(sb["file"]).exists():
        store.update_job(job["id"], {"status": "failed", "log": "配图方案文件缺失"})
        store.update_video(video["id"], {"status": "failed"})
        return

    story = store.get_story(video["story_id"]) or {}
    sb_data = sb.get("data") or {}
    if (story.get("audio_status") != "ready"
            or story.get("image_status") != "ready"
            or sb_data.get("audio_script_digest") != story.get("visual_plan_digest")):
        store.update_job(job["id"], {"status": "failed", "log": "音频或配图方案已失效"})
        store.update_video(video["id"], {"status": "failed"})
        return
    approved_audio = Path(story.get("audio_file", ""))
    audio_manifest = Path(story.get("audio_manifest", ""))
    if not approved_audio.is_file() or not audio_manifest.is_file():
        store.update_job(job["id"], {"status": "failed", "log": "已确认音频文件缺失"})
        store.update_video(video["id"], {"status": "failed"})
        return
    out_mp4 = ROOT / "output" / "videos" / f"{video['id']}.mp4"
    logfile = LOG_DIR / f"{job['id']}.log"
    texts = video.get("cover_texts") or {}
    cover_text = "|".join(f"{k}={v}" for k, v in texts.items())
    cmd = [str(PY), "-m", "pipeline.render", "--storyboard", sb["file"],
           "--out", str(out_mp4), "--cover-template", video.get("cover_template", "classic"),
           "--cover-text", cover_text, "--log-file", str(logfile),
           "--approved-audio", str(approved_audio),
           "--audio-manifest", str(audio_manifest),
           "--config", str(effective_config(story.get("series_id", "")))]
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        proc.wait()
    except Exception as e:
        store.update_job(job["id"], {"status": "failed", "log": str(e)})
        store.update_video(video["id"], {"status": "failed"})
        return

    log = logfile.read_text(encoding="utf-8", errors="replace") if logfile.exists() else ""
    if proc.returncode == 0 and out_mp4.exists():
        cover_file = Path((sb.get("data") or {}).get("cover_file", ""))
        try:
            cover_path = cover_file.resolve().relative_to(ROOT.resolve()).as_posix()
        except (ValueError, OSError):
            cover_path = ""
        store.update_job(job["id"], {"status": "done", "log": log})
        store.update_video(video["id"], {"status": "ready", "mp4": f"output/videos/{video['id']}.mp4",
                                         "cover": cover_path})
    else:
        store.update_job(job["id"], {"status": "failed", "log": log[-4000:]})
        store.update_video(video["id"], {"status": "failed"})


@app.on_event("startup")
def _startup():
    # 迁移 config.yaml 已有凭证到设置库（key 只需填一次）
    try:
        s = store.get_settings()
        changed = False
        cfg = load_config()
        img = s["image"]
        if img["providers"].get("siliconflow", {}).get("api_key") is None or img["providers"].get("siliconflow", {}).get("api_key") == "":
            img["providers"].setdefault("siliconflow", {})["api_key"] = cfg["image"]["api"].get("api_key", "")
            changed = True
        llm = s["llm"]
        if not (llm["providers"].get("deepseek", {}) or {}).get("api_key"):
            llm["providers"].setdefault("deepseek", {})["api_key"] = cfg["llm"].get("api_key", "")
            changed = True
        if changed:
            store.save_settings(s)
    except Exception as e:
        print("seed settings warn:", e)
    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=_batch_worker_loop, daemon=True).start()


def _create_video(story_id: str, cover_template: str = "classic",
                  cover_texts: dict | None = None) -> dict:
    """建封面并排入渲染队列（单条流程与批量流程共用）。"""
    story = store.get_story(story_id)
    if not story:
        raise HTTPException(404, "故事不存在")
    if story.get("audio_status") != "ready":
        raise HTTPException(400, "必须先试听并确认正式音频")
    if story.get("image_status") != "ready":
        raise HTTPException(400, "必须先生成并检查全部配图")
    sb = None
    for r in store.list_storyboards():
        if r["story_id"] == story_id:
            sb = r
    if not sb:
        raise HTTPException(400, "请先为该故事生成配图方案")
    data = dict(sb.get("data") or {})
    manifest_path = Path(story.get("audio_manifest", ""))
    audio_path = Path(story.get("audio_file", ""))
    if not manifest_path.is_file() or not audio_path.is_file():
        raise HTTPException(400, "已确认音频或时间轴文件缺失")
    try:
        from pipeline.visual_plan import validate_visual_plan
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_visual_plan(data, manifest)
    except Exception as exc:
        raise HTTPException(400, f"配图方案已失效：{exc}") from exc
    if data.get("audio_script_digest") != story.get("visual_plan_digest"):
        raise HTTPException(400, "配图方案与当前已确认音频不一致")
    image_files = [Path(path) for path in data.get("image_files") or []]
    if len(image_files) != len(data.get("scenes") or []) or not all(path.is_file() for path in image_files):
        raise HTTPException(400, "配图尚未全部生成")
    meta = series_meta(story.get("series_id"))
    texts = dict(cover_texts or {})
    texts.setdefault("badge", meta["badge"])
    texts.setdefault("subtitle", meta["subtitle"])
    # The selected cover is image one and consumes the title segment duration.
    cfg = load_config(str(effective_config(story.get("series_id", ""))))
    base_images = data.get("base_image_files") or data.get("image_files")
    cover_path = ROOT / "output" / "covers" / f"story_{story['id']}_{data['audio_script_digest'][:8]}.png"
    cover_mod.generate_cover(data, cfg, base_images[0], str(cover_path),
                             cover_template, texts)
    data["cover_template"] = cover_template
    data["cover_texts"] = texts
    data["cover_file"] = str(cover_path.resolve())
    data["image_files"][0] = str(cover_path.resolve())
    data["image_urls"][0] = _media_url(cover_path)
    Path(sb["file"]).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    sb = store.add_storyboard(story_id, data, sb["file"])
    video = store.add_video(story_id, sb["id"], cover_template, texts, {})
    job = _enqueue(video)
    return {"video": video, "job": job}


@app.post("/api/videos")
def api_video_create(body: VideoIn):
    return _create_video(body.story_id, body.cover_template, body.cover_texts)


@app.get("/api/videos")
def api_video_list():
    videos = store.list_videos()
    stories = {s["id"]: s for s in store.list_stories()}
    series_map = {s["id"]: s["name"] for s in store.list_series()}
    for v in videos:
        st = stories.get(v["story_id"], {})
        v["title"] = st.get("title", "")
        v["series_name"] = series_map.get(st.get("series_id", ""), "未分类")
    return videos


@app.get("/api/videos/{vid}")
def api_video_get(vid: str):
    v = store.get_video(vid)
    if not v:
        raise HTTPException(404, "视频不存在")
    return v


@app.post("/api/videos/{vid}/review")
def api_video_review(vid: str, body: ReviewIn):
    v = store.get_video(vid)
    if not v:
        raise HTTPException(404, "视频不存在")
    status = "approved" if body.approve else "rejected"
    return store.update_video(vid, {"status": status})


@app.post("/api/videos/{vid}/schedule")
def api_video_schedule(vid: str, body: ScheduleIn):
    v = store.get_video(vid)
    if not v:
        raise HTTPException(404, "视频不存在")
    return store.update_video(vid, {"status": "scheduled", "scheduled_date": body.date})


@app.post("/api/videos/{vid}/publish")
def api_video_publish(vid: str):
    v = store.get_video(vid)
    if not v:
        raise HTTPException(404, "视频不存在")
    return store.update_video(vid, {"status": "published", "scheduled_date": v.get("scheduled_date") or ""})


@app.get("/api/pool")
def api_pool():
    """待发布池：已通过/已排期/今日到期。"""
    videos = [v for v in store.list_videos() if v["status"] in ("approved", "scheduled", "published")]
    stories = {s["id"]: s for s in store.list_stories()}
    series_map = {s["id"]: s["name"] for s in store.list_series()}
    for v in videos:
        st = stories.get(v["story_id"], {})
        v["title"] = st.get("title", "")
        v["series_name"] = series_map.get(st.get("series_id", ""), "未分类")
    return videos


@app.get("/api/jobs")
def api_job_list():
    return store.list_jobs()


@app.get("/api/jobs/{jid}")
def api_job_get(jid: str):
    j = store.get_job(jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    return j


@app.get("/api/health")
def api_health():
    return {"ok": True}


# ------------------------------------------------------------ 静态/媒体 ----
@app.get("/media/{p:path}")
def media(p: str):
    f = (ROOT / "output" / p).resolve()
    if f.exists() and f.is_file():
        return FileResponse(f)
    raise HTTPException(404, "文件不存在")


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """静态资源不缓存（开发期改前端立即生效，避免旧版缓存）。"""
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")

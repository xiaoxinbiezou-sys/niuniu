/* 故事创作工作室前端 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

async function api(path, method = "GET", body, timeoutMs = 0) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const ctrl = new AbortController();
  let timer = null;
  if (timeoutMs > 0) {
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
    opt.signal = ctrl.signal;
  }
  try {
    const r = await fetch(path, opt);
    if (!r.ok) {
      let msg = "请求失败";
      try {
        const detail = (await r.json()).detail;
        if (typeof detail === "string") msg = detail;
        else if (detail?.message) msg = [detail.message, ...(detail.issues || [])].join("；");
      } catch (e) {}
      throw new Error(msg);
    }
    return r.json();
  } finally {
    if (timer) clearTimeout(timer);
  }
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// 存储路径 → media 可访问 URL：v.mp4 形如 "output/videos/x.mp4"，media 路由已含 output 前缀
function mediaUrl(p) {
  if (!p) return "";
  return "/media/" + String(p).replace(/^output\//, "");
}

const STATUS_ZH = {
  draft: "草稿", confirmed: "已确认", queued: "排队中", rendering: "制作中",
  ready: "待审核", approved: "已通过", rejected: "已驳回",
  rendered: "待试听", planned: "待生成配图",
  scheduled: "已排期", published: "已发布", failed: "失败",
};

/* ---------------- 页签 ---------------- */
const PAGES = ["create", "stories", "batch", "review", "pool", "series", "settings"];
function showPage(name) {
  PAGES.forEach((p) => {
    $(`#page-${p}`).classList.toggle("active", p === name);
    document.querySelector(`.tab[data-page="${p}"]`).classList.toggle("active", p === name);
  });
  if (name === "stories") loadStories();
  if (name === "review") loadReview();
  if (name === "pool") loadPool();
  if (name === "series") loadSeriesPage();
  if (name === "batch") loadBatchSeries();
  if (name === "settings") loadSettingsPage();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showPage(t.dataset.page)));

/* ---------------- 工具 ---------------- */
async function loadSeriesSelects() {
  const series = await api("/api/series");
  const fill = (sel) => {
    const s = $(sel);
    s.innerHTML = "";
    if (series.length === 0) {
      const o = el("option");
      o.textContent = "（请先到「系列」页新建系列）";
      s.appendChild(o);
    }
    series.forEach((x) => {
      const o = el("option");
      o.value = x.id;
      o.textContent = x.name;
      s.appendChild(o);
    });
  };
  fill("#crt-series");
  fill("#batch-series");
  const niuniu = series.find((x) => x.name === "牛牛一家人");
  if (niuniu) {
    $("#crt-series").value = niuniu.id;
    $("#batch-series").value = niuniu.id;
  }
  return series;
}

function badge(status) {
  const b = el("span", `badge ${status}`);
  b.textContent = STATUS_ZH[status] || status;
  return b;
}

function storyActions(st) {
  const wrap = el("div", "story-actions");
  const mk = (txt, fn) => {
    const b = el("button", "btn small", txt);
    b.addEventListener("click", fn);
    wrap.appendChild(b);
    return b;
  };
  mk("📖 看全文", (ev) => toggleStoryText(st, ev.target));
  if (st.status !== "confirmed") {
    mk("✏️ 编辑", () => promptEdit(st));
    mk("AI 重写", () => aiAction(st, "rewrite"));
    mk("AI 修改", () => aiAction(st, "modify"));
    mk("✅ 确认", async () => {
      await api(`/api/stories/${st.id}/confirm`, "POST", {});
      toast("故事已确认");
      loadStories();
    });
  } else {
    mk("继续制作", () => openStoryInStudio(st));
  }
  mk("🗑 删除", () => deleteStory(st));
  return wrap;
}

/* 在故事库里直接读全文：按需拉取，展开后不再重复请求 */
async function toggleStoryText(st, btn) {
  const card = btn.closest(".card");
  const existing = card.querySelector(".story-full");
  if (existing) {
    existing.remove();
    btn.textContent = "📖 看全文";
    return;
  }
  btn.disabled = true;
  btn.textContent = "读取中…";
  try {
    const full = await api(`/api/stories/${st.id}`);
    const text = (full.text || "").trim();
    const box = el("div", "story-full");
    const stat = el("div", "story-stat",
      `正文 ${text.replace(/\s/g, "").length} 字 · 点子：${full.idea || "（无）"}`);
    box.appendChild(stat);
    const body = el("div", "story-text");
    // 空行分段，段落之间留出呼吸感，方便手机上看
    text.split(/\n+/).forEach((p) => {
      if (p.trim()) body.appendChild(el("p", "", p.trim()));
    });
    box.appendChild(body);
    const foot = el("div", "hint");
    const facts = full.story_facts || {};
    const cast = (facts.characters || []).map((x) => (typeof x === "string" ? x : x.name)).filter(Boolean);
    foot.textContent = [
      `系列：${full.series_name || "未分类"}`,
      `类型：${MATERIAL_LABELS[full.material_type] || full.material_type || "-"}`,
      cast.length ? `人物：${cast.join("、")}` : "",
      full.created ? `创建：${full.created}` : "",
    ].filter(Boolean).join(" · ");
    box.appendChild(foot);
    card.appendChild(box);
    btn.textContent = "📖 收起";
  } catch (e) {
    toast("读取故事失败：" + e.message);
    btn.textContent = "📖 看全文";
  }
  btn.disabled = false;
}

async function deleteStory(st) {
  const warn = st.status === "confirmed"
    ? "\n\n注意：这条故事已经进入制作，它的配音、配图和成片也会一起删掉。"
    : "";
  if (!confirm(`确定删除《${st.title}》？${warn}`)) return;
  try {
    const r = await api(`/api/stories/${st.id}`, "DELETE");
    const n = (r.removed || []).length;
    toast(`已删除《${st.title}》${n ? `（清掉 ${n} 个文件）` : ""}`);
    loadStories();
  } catch (e) { toast("删除失败：" + e.message); }
}

async function deleteVideo(v, after) {
  if (!confirm(`确定删除成片《${v.title || v.id}》？\n\nMP4 文件会一起删掉，删了不能恢复。`)) return;
  try {
    await api(`/api/videos/${v.id}`, "DELETE");
    toast("已删除");
    after();
  } catch (e) { toast("删除失败：" + e.message); }
}

function promptEdit(st) {
  const text = prompt("编辑故事正文（标题留第一行：# 标题）", `# ${st.title}\n\n${st.text}`);
  if (!text) return;
  const lines = text.trim().split("\n").filter((l) => l.trim());
  const title = lines[0].replace(/^#\s*/, "").trim();
  const body = lines.slice(1).join("\n");
  api(`/api/stories/${st.id}/edit`, "POST", { text: body }).then(() => {
    toast("已保存");
    loadStories();
  }).catch((e) => toast(e.message));
}

async function aiAction(st, mode) {
  const label = mode === "rewrite" ? "重写反馈" : "修改指令";
  const input = prompt(`AI ${label}（如：${mode === "rewrite" ? "太短了，更生动一些" : "把主角改成小兔子"}）`);
  if (!input) return;
  await api(`/api/stories/${st.id}/${mode}`, "POST", mode === "rewrite" ? { feedback: input } : { instruction: input });
  toast("AI 已更新故事");
  loadStories();
}

/* ---------------- 创作页 ---------------- */
let crtStory = null;
let crtSb = null;
let crtTemplates = [];
const MATERIAL_TYPES = {
  auto: { label: "故事素材", rows: 4, placeholder: "输入故事点子或已有故事" },
  idea: { label: "故事点子", rows: 2, placeholder: "例如：牛牛梦见自己变成了巨人" },
  incomplete: { label: "未完成故事", rows: 10, placeholder: "输入已有开头或中段，AI 将保留内容并续写结局" },
  complete: { label: "较完整故事", rows: 12, placeholder: "输入完整故事，AI 只优化语言和节奏" },
};
const MATERIAL_LABELS = { auto: "自动判断", idea: "点子", incomplete: "不完整故事", complete: "较完整故事" };
const STORY_TYPE_LABELS = { auto: "自动判断", family: "家庭日常", imagination: "想象扩展", fantasy: "想象扩展" };

function updateMaterialTypeUI() {
  const type = $("#crt-material-type").value || "auto";
  const meta = MATERIAL_TYPES[type] || MATERIAL_TYPES.auto;
  $("#crt-material-label").textContent = meta.label;
  $("#crt-idea").rows = meta.rows;
  $("#crt-idea").placeholder = meta.placeholder;
}

/* ---------- 向导状态持久化（刷新不丢） ---------- */
function saveWizard() {
  const s = {};
  if (crtStory) {
    s.storyId = crtStory.id;
    s.storyTitle = $("#crt-title").value;
    s.storyText = $("#crt-text").value;
  }
  if (crtSb) s.sbId = crtSb.id;
  const t = document.querySelector(".tpl.active");
  if (t) s.template = t.dataset.templateId;
  s.coverFields = {};
  document.querySelectorAll("#crt-cover-fields input").forEach((i) => {
    s.coverFields[i.dataset.field] = i.value;
  });
  s.idea = $("#crt-idea").value;
  s.materialType = $("#crt-material-type").value;
  s.imageCount = $("#crt-image-count").value;
  s.storyType = $("#crt-story-type").value;
  localStorage.setItem("studio_wizard", JSON.stringify(s));
}

async function restoreWizard() {
  let s;
  try { s = JSON.parse(localStorage.getItem("studio_wizard") || "null"); } catch (e) { return; }
  if (!s) return;
  if (s.idea) $("#crt-idea").value = s.idea;
  $("#crt-material-type").value = s.materialType || "auto";
  $("#crt-story-type").value = s.storyType || "auto";
  if (s.imageCount) $("#crt-image-count").value = s.imageCount;
  updateMaterialTypeUI();
  if (s.storyId) {
    try {
      crtStory = await api(`/api/stories/${s.storyId}`);
      if (crtStory.series_id) $("#crt-series").value = crtStory.series_id;
      $("#crt-material-type").value = crtStory.input_type || "auto";
      $("#crt-story-type").value = crtStory.story_type_input || "auto";
      updateMaterialTypeUI();
      $("#crt-story-card").hidden = false;
      $("#crt-title").value = s.storyTitle || crtStory.title;
      $("#crt-text").value = s.storyText || crtStory.text;
      await refreshProductionUI();
    } catch (e) {}
  }
  if (s.template) {
    document.querySelectorAll(".tpl").forEach((c) => {
      c.classList.toggle("active", c.dataset.templateId === s.template);
    });
    const selectedTemplate = crtTemplates.find((template) => template.id === s.template);
    if (selectedTemplate) renderCoverFields(selectedTemplate);
  }
  if (s.coverFields) {
    document.querySelectorAll("#crt-cover-fields input").forEach((i) => {
      if (s.coverFields[i.dataset.field] !== undefined) i.value = s.coverFields[i.dataset.field];
    });
  }
  updateCoverPreview();
}

async function openStoryInStudio(story) {
  showPage("create");
  crtStory = await api(`/api/stories/${story.id}`);
  syncStoryCard();
  $("#crt-story-card").hidden = false;
  localStorage.setItem("studio_wizard", JSON.stringify({ storyId: crtStory.id }));
  await refreshProductionUI();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const EMOTION_LABELS = {
  narrate: "旁白叙述", neutral: "平淡（未推导出情绪）", mystery: "神秘", surprise: "惊讶",
  excited: "兴奋", laugh: "笑", awkward: "尴尬", cry: "哭", sad: "难过", scared: "害怕",
  angry: "生气", proud: "骄傲", triumph: "得意", tender: "温柔", question: "疑问",
  whisper: "小声", happy: "开心", brave: "勇敢", gentle: "柔和", sleepy: "困倦",
};

function showAudioScript(payload) {
  const report = payload.report || {};
  const metrics = report.metrics || {};
  const ratio = Math.round((metrics.narrator_ratio || 0) * 100);
  const neutralCount = metrics.neutral_dialogue_segments || 0;
  const emotionChips = Object.entries(metrics.dialogue_emotions || {})
    .map(([emo, n]) => `<span>${EMOTION_LABELS[emo] || emo}×${n}</span>`).join("");
  $("#crt-audio-report").innerHTML = `
    <span>旁白 ${ratio}%</span><span>对白 ${metrics.dialogue_segments || 0} 段</span>
    ${emotionChips}
    <span class="${report.ok ? "qa-ok" : "qa-bad"}">${report.ok ? "质量门通过" : "需要修改故事"}</span>`
    + (neutralCount
      ? `<span class="qa-bad">${neutralCount} 句台词没有情绪，会用平淡语气念</span>` : "");
  const box = $("#crt-audio-script-preview");
  box.innerHTML = "";
  (payload.script?.segments || []).forEach((segment) => {
    const row = el("div", `audio-line ${segment.type}`);
    row.appendChild(el("span", "audio-speaker", segment.speaker));
    const text = el("span", "audio-text", segment.text);
    if (segment.type === "dialogue") {
      const label = EMOTION_LABELS[segment.emotion] || segment.emotion || "";
      const tag = el("span", `emo-tag${segment.emotion === "neutral" ? " flat" : ""}`, label);
      text.appendChild(tag);
    }
    row.appendChild(text);
    box.appendChild(row);
  });
  (report.errors || []).forEach((issue) => box.appendChild(el("div", "qa-message bad", issue)));
  (report.warnings || []).forEach((issue) => box.appendChild(el("div", "qa-message", issue)));
  $("#crt-audio-approve").disabled = !report.ok || crtStory.audio_status !== "draft";
}

function showImages() {
  const box = $("#crt-image-grid");
  box.innerHTML = "";
  const urls = crtSb?.data?.image_urls || [];
  (crtSb?.data?.scenes || []).forEach((scene, index) => {
    const item = el("div", "image-item");
    if (urls[index]) {
      const img = el("img");
      img.src = `${urls[index]}?v=${Date.now()}`;
      img.alt = `配图 ${index + 1}`;
      item.appendChild(img);
    } else {
      item.appendChild(el("div", "image-placeholder", `${index + 1}`));
    }
    item.appendChild(el("div", "image-caption", `${index + 1}. ${scene.scene} · ${formatTime(scene.start)}-${formatTime(scene.end)}`));
    if (urls[index]) {
      const regen = el("button", "btn small", "重新生成这张");
      regen.onclick = async () => {
        regen.disabled = true;
        $("#crt-image-status").textContent = `正在重新生成第 ${index + 1} 张…`;
        try {
          crtSb = await api(`/api/storyboards/${crtSb.id}/images/regenerate`, "POST", { index }, 12 * 60 * 1000);
          showImages();
          $("#crt-image-status").textContent = "配图已更新";
        } catch (e) { toast(e.message); regen.disabled = false; }
      };
      item.appendChild(regen);
    }
    box.appendChild(item);
  });
  if (urls.length) {
    const base = crtSb.data.base_image_urls?.[0] || urls[0];
    $("#crt-cover-preview").style.backgroundImage = `url("${base}")`;
  }
}

function formatTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(value / 60);
  const s = Math.floor(value % 60).toString().padStart(2, "0");
  return `${m}:${s}`;
}

async function refreshProductionUI() {
  const confirmed = crtStory?.status === "confirmed";
  $("#crt-audio-card").hidden = !confirmed;
  $("#crt-sb-card").hidden = true;
  $("#crt-video-card").hidden = true;
  if (!confirmed) return;

  const state = crtStory.audio_status || "";
  const labels = { draft: "音频脚本待确认", approved: "音频脚本已确认", rendered: "音频已合成，请完整试听", ready: "音频已试听确认" };
  $("#crt-audio-state").textContent = labels[state] || "先生成旁白主导的音频脚本";
  $("#crt-audio-script").textContent = crtStory.audio_script ? "重新生成音频脚本" : "生成音频脚本";
  $("#crt-audio-approve").disabled = true;
  $("#crt-audio-render").disabled = !["approved", "rendered", "ready"].includes(state);
  $("#crt-audio-render").textContent = ["rendered", "ready"].includes(state) ? "重新合成音频" : "合成完整音频";
  $("#crt-storyboard").disabled = state !== "ready";
  $("#crt-audio-report").innerHTML = "";
  $("#crt-audio-script-preview").innerHTML = "";
  if (crtStory.audio_script) {
    try { showAudioScript(await api(`/api/stories/${crtStory.id}/audio-script`)); } catch (e) {}
  }
  const playerWrap = $("#crt-audio-player-wrap");
  playerWrap.hidden = !crtStory.audio_url;
  const player = $("#crt-audio-player");
  if (crtStory.audio_url) {
    if (player.getAttribute("src") !== crtStory.audio_url) player.src = crtStory.audio_url;
  } else if (player.getAttribute("src")) {
    // 播放器是共用的一个 <audio> 节点。切到"还没合成音频"的故事时，如果只把整条
    // 播放器藏起来而不清 src，上一个故事的 MP3 仍然挂在这个元素上，切回来就会
    // 听到别人的音频（实测：麻雀宝宝的故事播出了草莓故事的声音）。
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
  if (crtStory.audio_url) {
    const confirm = $("#crt-audio-confirm");
    confirm.hidden = state === "ready";
    confirm.disabled = state !== "rendered";
  }
  if (["planned", "ready"].includes(crtStory.image_status)) {
    try {
      crtSb = await api(`/api/stories/${crtStory.id}/storyboard`);
      showStoryboard();
      $("#crt-sb-card").hidden = false;
      $("#crt-video-card").hidden = crtStory.image_status !== "ready";
    } catch (e) {}
  }
}

function initCreate() {
  $("#crt-gen").onclick = async () => {
    const idea = $("#crt-idea").value.trim();
    if (!idea) return toast("先写个点子");
    $("#crt-gen").disabled = true;
    $("#crt-status").textContent = "AI 正在写故事…";
    try {
      crtStory = await api("/api/stories/generate", "POST", {
        idea,
        series_id: $("#crt-series").value,
        material_type: $("#crt-material-type").value,
        story_type: $("#crt-story-type").value,
      });
      $("#crt-status").textContent = "✅ 故事已生成";
      $("#crt-story-card").hidden = false;
      $("#crt-title").value = crtStory.title;
      $("#crt-text").value = crtStory.text;
      $("#crt-audio-card").hidden = true;
      $("#crt-sb-card").hidden = true;
      $("#crt-video-card").hidden = true;
      saveWizard();
    } catch (e) { toast(e.message); }
    $("#crt-gen").disabled = false;
  };
  $("#crt-rewrite").onclick = async () => {
    const f = $("#crt-feedback").value.trim();
    if (!f) return toast("填重写反馈");
    crtStory = await api(`/api/stories/${crtStory.id}/rewrite`, "POST", { feedback: f });
    syncStoryCard();
    await refreshProductionUI();
    saveWizard();
  };
  $("#crt-modify").onclick = async () => {
    const i = $("#crt-instruction").value.trim();
    if (!i) return toast("填修改指令");
    crtStory = await api(`/api/stories/${crtStory.id}/modify`, "POST", { instruction: i });
    syncStoryCard();
    await refreshProductionUI();
    saveWizard();
  };
  $("#crt-confirm").onclick = async () => {
    await api(`/api/stories/${crtStory.id}/edit`, "POST", {
      text: $("#crt-text").value,
      material_type: $("#crt-material-type").value,
      story_type: $("#crt-story-type").value,
      series_id: $("#crt-series").value,
    });
    crtStory = await api(`/api/stories/${crtStory.id}/confirm`, "POST", {});
    toast("故事已确认，下一步制作音频故事");
    await refreshProductionUI();
  };
  $("#crt-audio-script").onclick = async () => {
    $("#crt-audio-script").disabled = true;
    $("#crt-audio-state").textContent = "正在生成并校验音频脚本…";
    try {
      const result = await api(`/api/stories/${crtStory.id}/audio-script`, "POST", {});
      crtStory = result.story;
      showAudioScript(result);
      await refreshProductionUI();
    } catch (e) { toast(e.message); }
    $("#crt-audio-script").disabled = false;
  };
  $("#crt-audio-approve").onclick = async () => {
    try {
      const result = await api(`/api/stories/${crtStory.id}/audio-script/approve`, "POST", {});
      crtStory = result.story;
      showAudioScript(result);
      await refreshProductionUI();
      toast("音频脚本已冻结，可以合成音频");
    } catch (e) { toast(e.message); }
  };
  $("#crt-audio-render").onclick = async () => {
    $("#crt-audio-render").disabled = true;
    $("#crt-audio-state").textContent = "正在合成旁白、角色声音和配乐…";
    try {
      const result = await api(`/api/stories/${crtStory.id}/audio`, "POST", {}, 12 * 60 * 1000);
      crtStory = result.story;
      await refreshProductionUI();
      toast("完整音频已生成，请从头试听");
    } catch (e) { toast(e.message); }
    $("#crt-audio-render").disabled = false;
  };
  const markAudioListened = () => {
    if (crtStory?.audio_status === "rendered") $("#crt-audio-confirm").disabled = false;
  };
  $("#crt-audio-player").addEventListener("ended", markAudioListened);
  $("#crt-audio-player").addEventListener("timeupdate", (event) => {
    const player = event.currentTarget;
    if (player.duration && player.currentTime / player.duration >= 0.95) markAudioListened();
  });
  $("#crt-audio-confirm").onclick = async () => {
    try {
      const result = await api(`/api/stories/${crtStory.id}/audio/confirm`, "POST", {});
      crtStory = result.story;
      await refreshProductionUI();
      toast("音频已确认，可以规划配图");
    } catch (e) { toast(e.message); }
  };
  $("#crt-storyboard").onclick = async () => {
    $("#crt-storyboard").disabled = true;
    const count = parseInt($("#crt-image-count").value, 10) || 6;
    $("#crt-status").textContent = `正在把音频合并为最多 ${count} 个视觉节拍…`;
    try {
      crtSb = await api(`/api/stories/${crtStory.id}/storyboard`, "POST", { max_images: count });
      crtStory = await api(`/api/stories/${crtStory.id}`);
      showStoryboard();
      $("#crt-sb-card").hidden = false;
      $("#crt-status").textContent = "配图方案已生成";
    } catch (e) {
      toast("配图方案生成失败：" + e.message);
      $("#crt-status").textContent = "";
    }
    $("#crt-storyboard").disabled = false;
  };
  $("#crt-images").onclick = async () => {
    $("#crt-images").disabled = true;
    $("#crt-image-status").textContent = "正在生成配图，可能需要数分钟…";
    try {
      crtSb = await api(`/api/storyboards/${crtSb.id}/images`, "POST", {}, 12 * 60 * 1000);
      crtStory = await api(`/api/stories/${crtStory.id}`);
      showImages();
      $("#crt-video-card").hidden = false;
      $("#crt-image-status").textContent = "全部配图已生成";
      toast("配图已生成，可以渲染视频");
    } catch (e) { toast(e.message); $("#crt-image-status").textContent = ""; }
    $("#crt-images").disabled = false;
  };
  $("#crt-make").onclick = startMake;
  $("#crt-clear").onclick = () => {
    // 清空点子输入框 + AI 写的故事框，并重置向导状态
    $("#crt-idea").value = "";
    $("#crt-title").value = "";
    $("#crt-text").value = "";
    $("#crt-feedback").value = "";
    $("#crt-instruction").value = "";
    $("#crt-material-type").value = "auto";
    $("#crt-story-type").value = "auto";
    updateMaterialTypeUI();
    crtStory = null;
    crtSb = null;
    $("#crt-story-card").hidden = true;
    $("#crt-audio-card").hidden = true;
    $("#crt-sb-card").hidden = true;
    $("#crt-video-card").hidden = true;
    $("#crt-status").textContent = "";
    localStorage.removeItem("studio_wizard");
    toast("已清除内容");
  };
  $("#crt-title").addEventListener("input", () => { updateCoverTitle(); saveWizard(); });
  $("#crt-text").addEventListener("input", () => { crtStory.text = $("#crt-text").value; saveWizard(); });
  $("#crt-material-type").addEventListener("change", () => { updateMaterialTypeUI(); saveWizard(); });
  $("#crt-story-type").addEventListener("change", saveWizard);
  $("#crt-image-count").addEventListener("change", saveWizard);
}

function syncStoryCard() {
  $("#crt-material-type").value = crtStory.input_type || "auto";
  $("#crt-story-type").value = crtStory.story_type_input || "auto";
  updateMaterialTypeUI();
  $("#crt-title").value = crtStory.title;
  $("#crt-text").value = crtStory.text;
}

function showStoryboard() {
  const sb = crtSb.data;
  $("#sb-count").textContent = sb.scenes.length;
  const box = $("#sb-preview");
  box.innerHTML = "";
  sb.scenes.forEach((sc) => {
    const d = el("div", "sb-item");
    const head = el("div", "sb-item-head", `${sc.id}. ${sc.scene} · ${formatTime(sc.start)}-${formatTime(sc.end)}`);
    d.appendChild(head);
    d.appendChild(el("div", "sb-item-text", sc.narration));
    d.appendChild(el("div", "hint", `覆盖 ${(sc.segment_ids || []).length} 个音频段 · ${(sc.present || []).join("、") || "环境画面"}`));
    box.appendChild(d);
  });
  setupCoverTemplates();
  showImages();
  saveWizard();
}

function setupCoverTemplates() {
  const row = $("#crt-tpls");
  const selected = coverTemplate();
  row.innerHTML = "";
  crtTemplates.forEach((t) => {
    const active = t.id === (crtSb?.data?.cover_template || selected || "classic");
    const c = el("div", `tpl${active ? " active" : ""}`);
    c.dataset.templateId = t.id;
    c.innerHTML = `<div class="tname">${t.name}</div><div class="tdesc">${t.desc}</div>`;
    c.onclick = () => {
      row.querySelectorAll(".tpl").forEach((x) => x.classList.remove("active"));
      c.classList.add("active");
      renderCoverFields(t);
    };
    row.appendChild(c);
  });
  const current = crtTemplates.find((t) => t.id === coverTemplate()) || crtTemplates[0];
  renderCoverFields(current);
  updateCoverPreview();
}

function renderCoverFields(tpl) {
  const box = $("#crt-cover-fields");
  box.innerHTML = "";
  tpl.fields.forEach((f) => {
    const wrap = el("div");
    const lab = el("label", "", { badge: "徽章文字", title: "标题", subtitle: "副标题", tagline: "标语" }[f] || f);
    const inp = el("input");
    inp.dataset.field = f;
    inp.value = f === "title" ? (crtSb ? crtSb.data.title : "") : (tpl.defaults[f] || "");
    wrap.appendChild(lab);
    wrap.appendChild(inp);
    box.appendChild(wrap);
  });
  document.querySelectorAll("#crt-cover-fields input").forEach((i) => i.addEventListener("input", () => {
    updateCoverPreview();
    saveWizard();
  }));
  updateCoverTitle();
}

function updateCoverTitle() {
  const t = document.querySelector('#crt-cover-fields input[data-field="title"]');
  if (t) t.value = $("#crt-title").value;
  updateCoverPreview();
}

function coverTexts() {
  const out = {};
  document.querySelectorAll('#crt-cover-fields input').forEach((i) => { out[i.dataset.field] = i.value; });
  return out;
}

function updateCoverPreview() {
  const box = $("#crt-cover-preview");
  if (!box) return;
  const active = document.querySelector(".tpl.active");
  box.dataset.template = active?.dataset.templateId || "classic";
  const texts = coverTexts();
  box.querySelector(".cover-preview-badge").textContent = texts.badge || "儿童故事";
  box.querySelector(".cover-preview-title").textContent = texts.title || "故事标题";
  box.querySelector(".cover-preview-subtitle").textContent = texts.subtitle || "";
  box.querySelector(".cover-preview-tagline").textContent = texts.tagline || "";
}

function coverTemplate() {
  const a = document.querySelector(".tpl.active");
  return a?.dataset.templateId || "classic";
}

async function startMake() {
  if (!crtSb?.data?.image_files?.length) return toast("先生成全部配图");
  $("#crt-make").disabled = true;
  $("#crt-make-status").textContent = "已提交，正在排队制作…";
  try {
    const v = await api("/api/videos", "POST", {
      story_id: crtStory.id,
      cover_template: coverTemplate(),
      cover_texts: coverTexts(),
    });
    toast("制作任务已提交");
    pollVideo(v.video.id, $("#crt-make-status"));
  } catch (e) { toast(e.message); $("#crt-make").disabled = false; }
}

async function pollVideo(vid, statusEl) {
  for (let i = 0; i < 600; i++) {
    await new Promise((r) => setTimeout(r, 8000));
    const v = await api(`/api/videos/${vid}`);
    if (statusEl) statusEl.textContent = `状态：${STATUS_ZH[v.status] || v.status}`;
    if (v.status === "ready") { toast("成片完成，请到「审核」页查看"); break; }
    if (v.status === "failed") { toast("制作失败，查看任务日志"); break; }
  }
}

/* ---------------- 故事库 ---------------- */
async function loadStories() {
  const stories = await api("/api/stories");
  const box = $("#stories-list");
  box.innerHTML = "";
  if (!stories.length) { box.appendChild(el("div", "empty", "还没有故事，去「创作」页写一个吧")); return; }
  stories.forEach((st) => {
    const c = el("div", "card");
    const h = el("div");
    h.appendChild(el("span", "", `《${st.title}》`));
    h.appendChild(badge(st.status));
    const typeName = MATERIAL_LABELS[st.material_type] || st.material_type || "自动判断";
    const storyTypeName = STORY_TYPE_LABELS[st.story_type] || st.story_type || "自动判断";
    const meta = el("div", "hint", `${st.series_name || "未分类"} · ${typeName} · ${storyTypeName} · ${st.idea || ""} · ${st.created}`);
    h.appendChild(meta);
    c.appendChild(h);
    c.appendChild(storyActions(st));
    box.appendChild(c);
  });
}

/* ---------------- 批量排期 ---------------- */
async function loadBatchSeries() {
  const series = await api("/api/series");
  for (const sel of ["#batch-series", "#bb-series"]) {
    const s = $(sel);
    s.innerHTML = "";
    series.forEach((x) => {
      const o = el("option", "", x.name);
      o.value = x.id;
      s.appendChild(o);
    });
  }
}

let batchTimer = null;

/* 一键批量出片：贴点子 → 自动写故事/配音/配图/出片 */
function initOneClickBatch() {
  $("#bb-start").onclick = async () => {
    const ideas = $("#bb-ideas").value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!ideas.length) return toast("先写几个点子");
    if (ideas.length > 20) return toast("一次最多 20 条");
    if (!confirm(`将自动跑完 ${ideas.length} 条故事的全部制作流程（配音、配图、出片），\n每条约 3~5 分钟。中途不用管，确定开始吗？`)) return;
    $("#bb-start").disabled = true;
    $("#bb-status").textContent = "正在提交…";
    try {
      const b = await api("/api/batches", "POST", {
        ideas,
        series_id: $("#bb-series").value,
        image_count: parseInt($("#bb-image-count").value, 10) || 6,
        story_type: $("#bb-story-type").value,
      });
      toast(`已开始，共 ${b.items.length} 条`);
      $("#bb-ideas").value = "";
      loadBatches();
    } catch (e) {
      toast("启动失败：" + e.message);
      $("#bb-status").textContent = "";
    }
    $("#bb-start").disabled = false;
  };
  loadBatches();
  // 有任务在跑时每 5 秒刷新进度
  if (batchTimer) clearInterval(batchTimer);
  batchTimer = setInterval(loadBatches, 5000);
}

async function loadBatches() {
  let batches = [];
  try { batches = await api("/api/batches"); } catch (e) { return; }
  const box = $("#bb-batches");
  box.innerHTML = "";
  if (!batches.length) {
    $("#bb-status").textContent = "";
    return;
  }
  const running = batches.filter((b) => b.status === "queued" || b.status === "running");
  $("#bb-status").textContent = running.length ? `${running.length} 个批量任务进行中…` : "";
  batches.slice(0, 5).forEach((b) => {
    const card = el("div", "batch-card");
    const done = b.items.filter((i) => i.status === "done").length;
    const failed = b.items.filter((i) => i.status === "failed").length;
    const stateText = { queued: "排队中", running: "制作中", done: "已完成", failed: "全部失败" }[b.status] || b.status;
    const head = el("div", "batch-head");
    head.appendChild(el("strong", "", `批量任务 · ${b.items.length} 条`));
    head.appendChild(el("span", `badge ${b.status === "done" ? "confirmed" : b.status === "failed" ? "failed" : "rendering"}`, stateText));
    head.appendChild(el("span", "hint", `成功 ${done} · 失败 ${failed} · ${b.created}`));
    const del = el("button", "btn small", "🗑 删除记录");
    del.onclick = async () => {
      if (!confirm("删除这条批量任务的记录？（已生成的成片不会被删掉）")) return;
      try { await api(`/api/batches/${b.id}`, "DELETE"); loadBatches(); }
      catch (e) { toast(e.message); }
    };
    head.appendChild(del);
    card.appendChild(head);
    b.items.forEach((it) => {
      const row = el("div", "batch-item");
      const icon = { done: "✅", failed: "❌", running: "⏳", pending: "•" }[it.status] || "•";
      row.appendChild(el("span", "bi-icon", icon));
      const main = el("div", "bi-main");
      const titleLine = el("div", "bi-title", it.title || it.idea);
      main.appendChild(titleLine);
      const tail = it.status === "failed" ? it.error
        : (it.status === "running" ? `正在：${it.stage}` : "");
      if (tail) main.appendChild(el("div", "bi-stage", tail));
      row.appendChild(main);

      const actions = el("div", "bi-actions");
      (it.stages || []).length && actions.appendChild(stageButton(it));
      if (it.story_id) {
        const openStory = el("button", "btn small", "看故事");
        openStory.onclick = () => openStoryInStudio({ id: it.story_id, title: it.title });
        actions.appendChild(openStory);
      }
      if (it.video_id && it.status === "done") {
        const play = el("a", "btn small", "▶ 看成片");
        play.href = `/media/videos/${it.video_id}.mp4`;
        play.target = "_blank";
        actions.appendChild(play);
      }
      row.appendChild(actions);
      card.appendChild(row);
    });
    box.appendChild(card);
  });
}

/* 展开某一条的流水：写故事 → 确认母稿 → 编译脚本 → … → 成片 */
function stageButton(item) {
  const btn = el("button", "btn small", "过程节点");
  const panel = el("div", "stage-panel");
  panel.hidden = true;
  (item.stages || []).forEach((s) => {
    const line = el("div", `stage-line ${s.status}`);
    const mark = { done: "✅", failed: "❌", running: "⏳" }[s.status] || "•";
    line.appendChild(el("span", "stage-mark", mark));
    line.appendChild(el("span", "stage-name", s.name));
    line.appendChild(el("span", "stage-at", s.at || ""));
    panel.appendChild(line);
    if (s.detail) panel.appendChild(el("div", "stage-detail", s.detail));
  });
  btn.onclick = () => { panel.hidden = !panel.hidden; };
  const wrap = el("div");
  wrap.appendChild(btn);
  wrap.appendChild(panel);
  return wrap;
}

function initBatch() {
  $("#batch-gen").onclick = async () => {
    const ideas = $("#batch-ideas").value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!ideas.length) return toast("先写几个点子");
    $("#batch-gen").disabled = true;
    const box = $("#batch-progress");
    box.textContent = "";
    let done = 0;
    for (const idea of ideas) {
      try {
        await api("/api/stories/generate", "POST", {
          idea,
          series_id: $("#batch-series").value,
          material_type: "idea",
          story_type: $("#batch-story-type").value,
        });
        done++;
      } catch (e) { box.textContent += `失败: ${idea} — ${e.message}\n`; }
      box.textContent = `进度 ${done}/${ideas.length}…`;
    }
    box.textContent = `✅ 完成 ${done}/${ideas.length} 个故事`;
    $("#batch-gen").disabled = false;
    loadStories();
  };
  $("#batch-schedule").onclick = async () => {
    const daily = parseInt($("#batch-daily").value) || 1;
    const approved = (await api("/api/pool")).filter((v) => v.status === "approved");
    if (!approved.length) return toast("待发布池里没有已通过的视频");
    const date = new Date();
    for (let i = 0; i < approved.length; i++) {
      const day = Math.floor(i / daily);
      const d = new Date(date);
      d.setDate(d.getDate() + day);
      await api(`/api/videos/${approved[i].id}/schedule`, "POST", { date: d.toISOString().slice(0, 10) });
    }
    toast(`已为 ${approved.length} 条排期（每天 ${daily} 条）`);
    loadPool();
  };
}

/* ---------------- 审核 ---------------- */
async function loadReview() {
  const videos = (await api("/api/videos")).filter((v) => ["ready", "rejected", "failed", "rendering", "queued"].includes(v.status));
  const box = $("#review-list");
  box.innerHTML = "";
  if (!videos.length) { box.appendChild(el("div", "empty", "暂无待审核成片")); return; }
  videos.forEach((v) => {
    const c = el("div", "card video-card");
    const media = el("div");
    if (v.mp4) {
      const vid = el("video");
      vid.controls = true;
      vid.src = mediaUrl(v.mp4);
      media.appendChild(vid);
      if (v.cover) {
        const img = el("img");
        img.src = mediaUrl(v.cover);
        img.style.cssText = "width:100px;display:block;margin-top:8px;border-radius:6px";
        media.appendChild(img);
      }
    }
    const meta = el("div", "video-meta");
    const h = el("div", "title", `《${v.title || ""}》`);
    h.appendChild(badge(v.status));
    meta.appendChild(h);
    const t = el("div", "hint", `${v.series_name || "未分类"} · ${v.created}`);
    meta.appendChild(t);
    const act = el("div", "story-actions");
    const approve = el("button", "btn primary small", "✅ 通过 → 待发布池");
    const reject = el("button", "btn small", "❌ 驳回");
    const del = el("button", "btn small", "🗑 删除");
    approve.onclick = async () => { await api(`/api/videos/${v.id}/review`, "POST", { approve: true }); toast("已通过"); loadReview(); };
    reject.onclick = async () => { await api(`/api/videos/${v.id}/review`, "POST", { approve: false }); toast("已驳回"); loadReview(); };
    del.onclick = () => deleteVideo(v, loadReview);
    act.appendChild(approve);
    act.appendChild(reject);
    act.appendChild(del);
    meta.appendChild(act);
    c.appendChild(media);
    c.appendChild(meta);
    box.appendChild(c);
  });
}

/* ---------------- 待发布池 ---------------- */
async function loadPool() {
  const videos = await api("/api/pool");
  const box = $("#pool-list");
  box.innerHTML = "";
  if (!videos.length) { box.appendChild(el("div", "empty", "待发布池为空：成片审核通过后会自动进入这里")); return; }
  videos.forEach((v) => {
    const c = el("div", "card video-card");
    const media = el("div");
    if (v.mp4) {
      const vid = el("video");
      vid.controls = true;
      vid.src = mediaUrl(v.mp4);
      media.appendChild(vid);
    }
    const meta = el("div", "video-meta");
    const h = el("div", "title", `《${v.title || ""}》`);
    h.appendChild(badge(v.status));
    meta.appendChild(h);
    const t = el("div", "hint", `${v.series_name || "未分类"} · ${v.created}`);
    meta.appendChild(t);
    const row = el("div", "story-actions");
    const dateIn = el("input");
    dateIn.type = "date";
    dateIn.value = v.scheduled_date || "";
    const setDate = el("button", "btn small", "📅 排期");
    setDate.onclick = async () => {
      if (!dateIn.value) return toast("选个日期");
      await api(`/api/videos/${v.id}/schedule`, "POST", { date: dateIn.value });
      toast("已排期");
      loadPool();
    };
    const pub = el("button", "btn small", "🚀 标记已发布");
    pub.onclick = async () => { await api(`/api/videos/${v.id}/publish`, "POST", {}); toast("已标记发布"); loadPool(); };
    const del = el("button", "btn small", "🗑 删除");
    del.onclick = () => deleteVideo(v, loadPool);
    row.appendChild(dateIn);
    row.appendChild(setDate);
    row.appendChild(pub);
    row.appendChild(del);
    meta.appendChild(row);
    c.appendChild(media);
    c.appendChild(meta);
    box.appendChild(c);
  });
}

/* ---------------- 系列 ---------------- */
async function loadSeriesPage() {
  const series = await api("/api/series");
  const tplSel = $("#ser-tpl");
  tplSel.innerHTML = "";
  crtTemplates.forEach((t) => {
    const o = el("option", "", t.name);
    o.value = t.id;
    tplSel.appendChild(o);
  });
  const box = $("#series-list");
  box.innerHTML = "";
  if (!series.length) { box.appendChild(el("div", "empty", "还没有系列，先建一个（如：绘本故事 / 拟人卡通）")); return; }
  series.forEach((s) => {
    const c = el("div", "card series-card");
    const info = el("div");
    info.appendChild(el("div", "title", s.name));
    const bible = s.series_bible ? ` · 圣经=${s.series_bible}` : " · 未配置系列圣经";
    info.appendChild(el("div", "hint", `${s.style === "cartoon" ? "拟人卡通" : "水彩绘本"} · 封面模板[${s.cover_template}] · bgm=${s.bgm_mood}${bible}`));
    c.appendChild(info);
    box.appendChild(c);
  });
}

function initSeries() {
  $("#ser-add").onclick = async () => {
    const name = $("#ser-name").value.trim();
    if (!name) return toast("填系列名");
    await api("/api/series", "POST", {
      name,
      style: $("#ser-style").value,
      cover_template: $("#ser-tpl").value,
      bgm_mood: $("#ser-bgm").value,
      badge: $("#ser-badge").value.trim(),
      subtitle: $("#ser-sub").value.trim(),
      series_bible: $("#ser-bible").value.trim(),
    });
    toast("系列已创建");
    $("#ser-name").value = "";
    loadSeriesPage();
    loadSeriesSelects();
  };
}

/* ---------------- 设置中心 ---------------- */
let settingsState = {};
let voiceCatalogAll = { qwen: [], doubao: [], edge: [] };
const ROLE_NAMES = ["旁白", "牛牛", "添添", "爸爸", "妈妈"];
const CAST_NAMES = ["牛牛", "添添", "爸爸", "妈妈"];

function initSettings() {
  document.querySelectorAll(".sub-tab").forEach((t) => {
    t.addEventListener("click", () => {
      document.querySelectorAll(".sub-tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".sub-page").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $(`#sub-${t.dataset.sub}`).classList.add("active");
    });
  });
  $("#prev-engine").addEventListener("change", () => fillVoiceSelect("#prev-voice", $("#prev-engine").value));
  $("#prev-play").addEventListener("click", async () => {
    const engine = $("#prev-engine").value;
    const voice = $("#prev-voice").value;
    const emotion = $("#prev-cry")?.checked ? "cry" : "";
    const text = emotion ? "呜……我最喜欢的积木倒了，我好难过……" : "你好，今天我们来讲一个牛牛一家的故事。";
    const r = await api("/api/voices/preview", "POST", { engine, voice, voice_type: voice, emotion, text });
    $("#prev-audio").innerHTML = `<audio controls autoplay src="${r.url}"></audio>`;
  });
  $("#settings-save").addEventListener("click", saveSettings);
}

async function loadSettingsPage() {
  settingsState = await api("/api/settings");
  voiceCatalogAll = await api("/api/voice-catalog");
  // 模型
  $("#key-deepseek").value = settingsState.llm.providers.deepseek?.api_key || "";
  $("#key-qwen").value = settingsState.llm.providers.qwen?.api_key || "";
  $("#key-doubao").value = settingsState.llm.providers.doubao?.api_key || "";
  $("#key-glm").value = settingsState.llm.providers.glm?.api_key || "";
  $("#key-kimi").value = settingsState.llm.providers.kimi?.api_key || "";
  fillModelSelects();
  // 声音
  $("#vkey-qwen").value = settingsState.voice.providers.qwen?.api_key || "";
  $("#vkey-doubao").value = settingsState.voice.providers.doubao?.x_api_key || "";
  $("#vkey-doubao-appid").value = settingsState.voice.providers.doubao?.appid || "";
  $("#vkey-doubao-token").value = settingsState.voice.providers.doubao?.access_token || "";
  renderVoicePresetList();
  fillVoiceSelect("#prev-voice", $("#prev-engine").value);
  buildRoleVoices();
  // 图片
  const img = settingsState.image;
  $("#img-provider").value = img.provider || "doubao";
  const ip = img.providers[img.provider] || {};
  $("#img-key").value = ip.api_key || "";
  $("#img-model").value = ip.model || "";
  $("#img-count-default").value = String(img.story_image_count || 6);
  buildCastEditor();
}

function fillModelSelects() {
  const provs = ["deepseek", "qwen", "doubao", "glm", "kimi"];
  const names = { deepseek: "DeepSeek", qwen: "千问（百炼）", doubao: "豆包（方舟）", glm: "智谱", kimi: "Kimi K3" };
  ["sel-family-story-model", "sel-imagination-story-model", "sel-script-model"].forEach((selId) => {
    const sel = $(`#${selId}`);
    sel.innerHTML = "";
    provs.forEach((p) => {
      const o = el("option", "", names[p]);
      o.value = p;
      sel.appendChild(o);
    });
  });
  $("#sel-family-story-model").value = settingsState.llm.family_story_model || settingsState.llm.story_model || "deepseek";
  $("#sel-imagination-story-model").value = settingsState.llm.imagination_story_model || settingsState.llm.fantasy_story_model || settingsState.llm.story_model || "deepseek";
  $("#sel-script-model").value = settingsState.llm.script_model || "deepseek";
}

function fillVoiceSelect(selId, engine) {
  const sel = $(selId);
  sel.innerHTML = "";
  const list = voiceCatalogAll[engine] || [];
  list.forEach(([v, name]) => {
    const o = el("option", "", name);
    o.value = v;
    sel.appendChild(o);
  });
}

function voiceConfigValue(cfg) {
  return cfg?.edge_voice || cfg?.voice || cfg?.voice_type || "";
}

function voiceDisplayName(cfg) {
  const engine = cfg?.engine || "";
  const value = voiceConfigValue(cfg);
  const labels = { qwen: "千问", doubao: "豆包", edge: "Edge" };
  const found = (voiceCatalogAll[engine] || []).find(([id]) => id === value);
  return `${labels[engine] || engine}${found ? ` · ${found[1]}` : value ? ` · ${value}` : ""}`;
}

function renderVoicePresetList() {
  const box = $("#voice-preset-list");
  box.innerHTML = "";
  const presets = settingsState.voice.presets || {};
  const ordered = Object.entries(presets).sort(([a], [b]) => (a === "preset2" ? -1 : b === "preset2" ? 1 : a.localeCompare(b)));
  ordered.forEach(([id, preset]) => {
    const active = id === settingsState.voice.active_preset;
    const item = el("div", `voice-preset${active ? " active" : ""}`);
    const head = el("div", "voice-preset-head");
    head.appendChild(el("strong", "", preset.name || id));
    head.appendChild(el("span", `badge${active ? " confirmed" : ""}`, active ? "当前制作方案" : "备用"));
    item.appendChild(head);
    const roles = preset.roles || {};
    ROLE_NAMES.forEach((role) => {
      const line = el("div", "voice-preset-role");
      line.appendChild(el("span", "", role));
      line.appendChild(el("span", "", voiceDisplayName(roles[role] || {})));
      item.appendChild(line);
    });
    if (!active) {
      const apply = el("button", "btn small", "设为当前方案");
      apply.onclick = async () => {
        await api("/api/settings", "PUT", { voice: { active_preset: id } });
        settingsState.voice.active_preset = id;
        renderVoicePresetList();
        buildRoleVoices();
        toast("当前制作声音方案已切换");
      };
      item.appendChild(apply);
    }
    box.appendChild(item);
  });
}

function buildRoleVoices() {
  const box = $("#role-voices");
  box.innerHTML = "";
  const vset = settingsState.voice;
  const presets = vset.presets || {};
  const active = presets[vset.active_preset] || {};
  const roles = active.roles || vset.roles || {};
  $("#role-voices-title").textContent = `${active.name || "当前方案"} · 角色声音`;
  ROLE_NAMES.forEach((name) => {
    const row = el("div", "voice-row");
    row.appendChild(el("span", "vname", name));
    const engSel = el("select");
    // 只列出真正可用的引擎：voice-catalog 里没有 clone，本地的 GPT-SoVITS 服务
    // 也不会跟着 Studio 一起启动，选了它音色列表是空的、合成必然失败。
    ["qwen", "doubao", "edge"].forEach((e) => {
      const o = el("option", "", { qwen: "千问", doubao: "豆包", edge: "Edge" }[e]);
      o.value = e;
      engSel.appendChild(o);
    });
    const vSel = el("select");
    const cfg = roles[name] || {};
    engSel.value = cfg.engine || (name === "旁白" ? "qwen" : "doubao");
    fillVoiceSelectByEl(vSel, engSel.value, voiceConfigValue(cfg));
    engSel.onchange = () => { fillVoiceSelectByEl(vSel, engSel.value); saveRoleVoice(name, engSel.value, vSel.value); };
    vSel.onchange = () => saveRoleVoice(name, engSel.value, vSel.value);
    const prev = el("button", "btn", "🔊 试听");
    prev.onclick = () => {
      const cry = name === "牛牛";
      const text = cry ? "呜……我最喜欢的积木倒了，我好难过……" : `我是${name}，今天我们一起玩吧！`;
      api("/api/voices/preview", "POST", { engine: engSel.value, voice: vSel.value, voice_type: vSel.value, emotion: cry ? "cry" : "", text })
        .then((r) => new Audio(r.url).play());
    };
    row.appendChild(engSel);
    row.appendChild(vSel);
    row.appendChild(prev);
    box.appendChild(row);
  });
}

function fillVoiceSelectByEl(sel, engine, selected = "") {
  sel.innerHTML = "";
  (voiceCatalogAll[engine] || []).forEach(([v, name]) => {
    const o = el("option", "", name);
    o.value = v;
    sel.appendChild(o);
  });
  if (selected && ![...sel.options].some((option) => option.value === selected)) {
    const saved = el("option", "", `${selected}（已保存音色）`);
    saved.value = selected;
    sel.appendChild(saved);
  }
  if (selected) sel.value = selected;
}

function saveRoleVoice(name, engine, voice) {
  const vset = settingsState.voice;
  const presets = vset.presets || {};
  const pid = vset.active_preset || "preset2";
  presets[pid] = presets[pid] || { name: pid, roles: {} };
  const current = presets[pid].roles[name] || {};
  const next = { engine };
  if (name === "旁白" && current.narrate_instruct) next.narrate_instruct = current.narrate_instruct;
  if (name === "牛牛" && current.cry_instruct) next.cry_instruct = current.cry_instruct;
  delete next.voice;
  delete next.voice_type;
  delete next.edge_voice;
  if (engine === "edge") next.edge_voice = voice;
  if (engine === "qwen") next.voice = voice;
  if (engine === "doubao") next.voice_type = voice;
  presets[pid].roles[name] = next;
  renderVoicePresetList();
}

async function buildCastEditor() {
  const refs = await api("/api/refs");
  const box = $("#cast-editor");
  box.innerHTML = "";
  const cast = settingsState.image.cast || {};
  const imgRefs = settingsState.image.refs || {};
  CAST_NAMES.forEach((name) => {
    const wrap = el("div", "cast-row");
    const h = el("div", "", `${name} 形象卡`);
    h.style.fontWeight = "600";
    const ta = el("textarea", "", cast[name] || "");
    ta.placeholder = "如：黑发圆脸的5岁中国小男孩，穿黄色T恤和蓝色背带裤，比姐姐矮一个头";
    // 参考图选择
    const sel = el("select");
    const none = el("option", "", "（不用参考图）");
    none.value = "";
    sel.appendChild(none);
    refs.forEach((r) => {
      const o = el("option", "", r.name);
      o.value = r.name;
      sel.appendChild(o);
    });
    sel.value = imgRefs[name] || "";
    const prevImg = el("img");
    prevImg.className = "cast-img";
    if (sel.value) prevImg.src = `/ref/${sel.value}`;
    sel.onchange = () => {
      settingsState.image.refs = settingsState.image.refs || {};
      settingsState.image.refs[name] = sel.value;
      prevImg.src = sel.value ? `/ref/${sel.value}` : "";
    };
    const gen = el("button", "btn", "🎨 生成定妆照");
    const imgBox = el("div");
    gen.onclick = async () => {
      gen.disabled = true;
      const prompt = `卡通2D扁平风格，角色定妆照，${ta.value}，正面全身，纯色背景，可爱绘本感，竖构图`;
      const r = await api("/api/images/preview", "POST", { prompt, ref: sel.value });
      imgBox.innerHTML = `<img class="cast-img" src="${r.url}"><button class="btn small">✅ 使用此形象</button>`;
      imgBox.querySelector("button").onclick = () => {
        settingsState.image.cast[name] = ta.value;
        settingsState.image.refs = settingsState.image.refs || {};
        settingsState.image.refs[name] = sel.value;
        toast(`已锁定 ${name} 的形象卡 + 参考图`);
      };
      gen.disabled = false;
    };
    const refRow = el("div", "row");
    refRow.appendChild(prevImg);
    wrap.appendChild(h);
    wrap.appendChild(ta);
    const pickRow = el("div", "row");
    pickRow.appendChild(sel);
    pickRow.appendChild(gen);
    wrap.appendChild(refRow);
    wrap.appendChild(pickRow);
    wrap.appendChild(imgBox);
    box.appendChild(wrap);
  });
}

async function saveSettings() {
  // 模型
  settingsState.llm.providers = {
    deepseek: { api_key: $("#key-deepseek").value.trim() },
    qwen: { api_key: $("#key-qwen").value.trim() },
    doubao: { api_key: $("#key-doubao").value.trim() },
    glm: { api_key: $("#key-glm").value.trim() },
    kimi: { api_key: $("#key-kimi").value.trim() },
  };
  settingsState.llm.family_story_model = $("#sel-family-story-model").value;
  settingsState.llm.imagination_story_model = $("#sel-imagination-story-model").value;
  settingsState.llm.story_model = settingsState.llm.family_story_model;
  settingsState.llm.script_model = $("#sel-script-model").value;
  // 声音
  settingsState.voice.providers.qwen = { api_key: $("#vkey-qwen").value.trim() };
  settingsState.voice.providers.doubao = {
    x_api_key: $("#vkey-doubao").value.trim(),
    appid: $("#vkey-doubao-appid").value.trim(),
    access_token: $("#vkey-doubao-token").value.trim(),
    resource_id: "seed-tts-2.0"
  };
  // 图片
  const img = settingsState.image;
  img.provider = $("#img-provider").value;
  img.providers[img.provider] = { ...(img.providers[img.provider] || {}), api_key: $("#img-key").value.trim(), model: $("#img-model").value.trim() };
  img.story_image_count = parseInt($("#img-count-default").value, 10) || 6;
  await api("/api/settings", "PUT", settingsState);
  toast("✅ 设置已保存");
}

/* ---------------- 启动 ---------------- */
(async function boot() {
  crtTemplates = await api("/api/templates");
  await loadSeriesSelects();
  initCreate();
  initBatch();
  initOneClickBatch();
  initSeries();
  initSettings();
  // 页面上的「刷新」按钮（以前没有绑定，点了没反应）
  $("#stories-refresh").onclick = () => loadStories();
  $("#review-refresh").onclick = () => loadReview();
  $("#pool-refresh").onclick = () => loadPool();
  // 设置中心里配的默认配图数量，作为创作页的初始值
  try {
    const st = await api("/api/settings");
    const configured = String((st.image || {}).story_image_count || 6);
    const picker = $("#crt-image-count");
    if ([...picker.options].some((o) => o.value === configured)) picker.value = configured;
  } catch (e) {}
  await restoreWizard();
  // 任务数轮询
  setInterval(async () => {
    try {
      const jobs = await api("/api/jobs");
      const running = jobs.filter((j) => j.status === "running").length;
      const queued = jobs.filter((j) => j.status === "queued").length;
      $("#jobBadge").textContent = running || queued ? `⏳ 制作中 ${running} · 排队 ${queued}` : "";
    } catch (e) {}
  }, 15000);
})();

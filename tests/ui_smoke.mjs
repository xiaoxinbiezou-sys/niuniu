import fs from "node:fs";

const port = Number(process.argv[2] || 9228);
const outDir = process.argv[3] || "output/ui-smoke";
const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = tabs.find((tab) => tab.type === "page");
if (!page) throw new Error("No browser page found");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
let nextId = 0;
const pending = new Map();
ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (!msg.id || !pending.has(msg.id)) return;
  const { resolve, reject } = pending.get(msg.id);
  pending.delete(msg.id);
  if (msg.error) reject(new Error(msg.error.message));
  else resolve(msg.result);
});
function send(method, params = {}) {
  const id = ++nextId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

await send("Page.enable");
await send("Runtime.enable");
await send("Page.navigate", { url: "http://127.0.0.1:8000/" });
await new Promise((resolve) => setTimeout(resolve, 1800));
await evaluate(`(() => {
  document.querySelector('#crt-story-card').hidden = false;
  document.querySelector('#crt-audio-card').hidden = false;
  document.querySelector('#crt-sb-card').hidden = false;
  document.querySelector('#crt-video-card').hidden = false;
  document.querySelector('#crt-title').value = '恐龙洗澡大作战';
  document.querySelector('#crt-text').value = '牛牛不肯洗澡，说自己是恐龙。爸爸拿出水枪，陪他完成了一场洗澡大战。';
  document.querySelector('#crt-audio-state').textContent = '音频已试听确认';
  document.querySelector('#crt-audio-report').innerHTML = '<span>旁白 74%</span><span>对白 4 段</span><span class="qa-ok">质量门通过</span>';
  document.querySelector('#crt-audio-script-preview').innerHTML = [
    ['旁白','洗澡时间到了，牛牛却抱着门框不肯进去。'],
    ['牛牛','我是恐龙！恐龙不洗澡！'],
    ['旁白','爸爸想了想，从门后拿出一把水枪。'],
    ['爸爸','那我来抓住这只脏恐龙。']
  ].map(([s,t],i) => '<div class="audio-line '+(i%2?'dialogue':'narrator')+'"><span class="audio-speaker">'+s+'</span><span class="audio-text">'+t+'</span></div>').join('');
  document.querySelector('#crt-audio-player-wrap').hidden = false;
  document.querySelector('#crt-audio-confirm').hidden = true;
  document.querySelector('#sb-count').textContent = '8';
  document.querySelector('#sb-preview').innerHTML = Array.from({length:8},(_,i) => '<div class="sb-item"><div class="sb-item-head">'+(i+1)+'. 家里卫生间 · 0:'+(i*12).toString().padStart(2,'0')+'-0:'+((i+1)*12).toString().padStart(2,'0')+'</div><div class="sb-item-text">牛牛和爸爸进行水枪洗澡大战。</div><div class="hint">覆盖 3 个音频段 · 牛牛、爸爸</div></div>').join('');
  document.querySelector('#crt-image-grid').innerHTML = Array.from({length:8},(_,i) => '<div class="image-item"><div class="image-placeholder">'+(i+1)+'</div><div class="image-caption">'+(i+1)+'. 家里卫生间 · 0:00-0:12</div><button class="btn small">重新生成这张</button></div>').join('');
  document.querySelector('#crt-tpls').innerHTML = '<div class="tpl active" data-template-id="classic"><div class="tname">经典标题</div><div class="tdesc">中央大标题</div></div><div class="tpl"><div class="tname">绘本边框</div><div class="tdesc">温暖画框</div></div>';
  document.querySelector('#crt-cover-fields').innerHTML = '<div><label>徽章文字</label><input value="牛牛一家人"></div><div><label>标题</label><input value="恐龙洗澡大作战"></div>';
  document.querySelector('.cover-preview-badge').textContent = '牛牛一家人';
  document.querySelector('.cover-preview-title').textContent = '恐龙洗澡大作战';
})()`);

fs.mkdirSync(outDir, { recursive: true });
const viewports = [{ name: "desktop", width: 1280, height: 900 }, { name: "mobile", width: 390, height: 844 }];
const reports = [];
for (const viewport of viewports) {
  await send("Emulation.setDeviceMetricsOverride", { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.width < 500 });
  await new Promise((resolve) => setTimeout(resolve, 300));
  const metrics = await evaluate(`(() => ({
    innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    visibleStages: [...document.querySelectorAll('.stage-number')].filter(x => x.offsetParent).map(x => x.textContent),
    imageColumns: getComputedStyle(document.querySelector('#crt-image-grid')).gridTemplateColumns,
    audioWidth: Math.round(document.querySelector('#crt-audio-card').getBoundingClientRect().width)
  }))()`);
  const shot = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  fs.writeFileSync(`${outDir}/${viewport.name}.png`, Buffer.from(shot.data, "base64"));
  reports.push({ ...viewport, ...metrics });
}
console.log(JSON.stringify(reports, null, 2));
ws.close();

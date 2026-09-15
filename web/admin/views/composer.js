// Reels studio: pick an analysed local video, choose a cut, frame it, caption it, render branded shorts.
import { api, post, del, esc, attr, icon, ts, r1, toast, confirmDialog, emptyState } from '../core.js';

export const title = 'Reels studio';
export const subtitle = 'analysed video → proposed cut → branded short';
let root, ctx, SYSTEM = null, JOBS = [], JOBSIG = '', SEL = null, JOB = null, CUTS = null, CUT = null, DUR = 0, TPL = 'placeholder', RENDERS = [];
const ED_FRAMING = () => ({ fit: 'auto', focus_x: 0.5, focus_y: 0.5 });
let ED = blankEd(), FR = null, FOCUS_MANUAL = false, FR_REQ = 0, FR_TIMER = null, FR_IMG_KEY = '', LOOP = false, keyHandler = null;
function blankEd() { return { cut_in: 0, cut_out: 0, captions: [], captions_enabled: true, lower_third: { name: '', role: '' }, title: '', cut_id: null, ...ED_FRAMING() }; }
const $ = (s) => root && root.querySelector(s);
const $$ = (s) => root ? [...root.querySelectorAll(s)] : [];

export async function render(el, params, c) {
  root = el; ctx = c; SEL = null; JOB = null; RENDERS = []; JOBSIG = '';
  try { SYSTEM = await api('/composer/system'); } catch (e) { root.innerHTML = `<div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div>`; return; }
  if (!SYSTEM.enabled) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('movie', 'The Reels studio is switched off', 'set COMPOSER_ENABLED=true in .env and restart the API')}</div></div>`; return; }
  TPL = SYSTEM.template;
  root.innerHTML = `<div class="studio">
    <div class="stack"><div class="card"><div class="hd"><h3>Analysed videos</h3><span class="sp"></span><span class="tag" id="jobcount"></span></div><div class="bd flush list" id="jobs"><div class="loading"><span class="spin"></span></div></div></div>
      <div class="card"><div class="hd"><h3>Template &amp; output</h3></div><div class="bd"><select id="tpl"></select>
        <div class="chips" style="margin-top:12px" id="presets"><label class="chip on"><input type="checkbox" value="reels" checked hidden> Reels 9:16</label><label class="chip"><input type="checkbox" value="square" hidden> Feed 1:1</label><label class="chip"><input type="checkbox" value="landscape" hidden> YouTube 16:9</label></div>
        <img id="tplprev" class="tplprev" alt="template preview" hidden>
        <details style="margin-top:12px"><summary>Upload a real layer (PNG with alpha / .mov 4444 / .webm)</summary>
          <div class="stack" style="gap:8px;margin-top:8px"><div class="field"><label>Template name</label><input type="text" id="upname" placeholder="cimage"></div>
          <div class="row gap16"><div class="field grow"><label>Preset</label><select id="uppreset"><option value="reels">reels</option><option value="square">square</option><option value="landscape">landscape</option></select></div><div class="field grow"><label>Layer name</label><input type="text" id="uplayer" value="frame"></div></div>
          <div class="row gap16"><div class="field grow"><label>x</label><input type="number" id="upx" value="0"></div><div class="field grow"><label>y</label><input type="number" id="upy" value="0"></div><div class="field grow"><label>z</label><input type="number" id="upz" value="0"></div></div>
          <label class="check"><input type="checkbox" id="upreplace" checked> replace all existing layers of this preset</label>
          <input type="file" id="upfile" accept=".png,.mov,.webm"><button class="btn tonal sm" id="upgo">${icon('upload', 's')}Upload</button><div class="msg" id="upmsg"></div></div></details>
      </div></div></div>
    <div class="card" id="editor"><div class="bd">${emptyState('cut', 'Pick an analysed video with a local file', 'YouTube-sourced videos have no file to cut')}</div></div>
    <div class="card rcol"><div class="hd"><h3>Renders</h3><span class="sp"></span><span class="tag" id="rcount"></span></div><div class="bd" id="renders"><div class="empty">No renders yet.</div></div></div>
  </div>`;
  renderTemplates(); await refreshJobs();
  $$('#presets .chip').forEach((l) => l.onclick = (e) => { e.preventDefault(); const cb = l.querySelector('input'); cb.checked = !cb.checked; l.classList.toggle('on', cb.checked); previewTemplate(); syncRenderButton(); if (SEL) scheduleFraming(false, 0); });
  $('#upgo').onclick = uploadLayer;
  keyHandler = onKey; document.addEventListener('keydown', keyHandler);
  const want = params[0]; if (want && JOBS.find((j) => j.id === want)) select(want); else if (want) toast('That video has no local file to cut', true);
}
export async function tick() { if (!root || !SYSTEM || !SYSTEM.enabled) return; await refreshJobs(); await refreshRenders(false); }
export function destroy() { if (keyHandler) document.removeEventListener('keydown', keyHandler); keyHandler = null; clearTimeout(FR_TIMER); root = null; SEL = null; }

const selectedPresets = () => $$('#presets input:checked').map((c) => c.value);
function renderTemplates() { const sel = $('#tpl'); sel.innerHTML = (SYSTEM.templates || []).map((t) => `<option value="${attr(t.name)}" ${t.name === TPL ? 'selected' : ''}>${esc(t.name)}${t.error ? ' (broken)' : ''}${t.missing_files && t.missing_files.length ? ' (missing files)' : ''}</option>`).join(''); sel.onchange = () => { TPL = sel.value; previewTemplate(); syncRenderButton(); if (SEL) scheduleFraming(false, 0); }; previewTemplate(); }
function previewTemplate() { const p = selectedPresets()[0] || 'reels'; const img = $('#tplprev'); img.hidden = false; img.src = '/api/v1/composer/templates/' + encodeURIComponent(TPL) + '/preview?preset=' + p + '&t=' + Date.now(); }

// ---------------------------------------------------------------- jobs
async function refreshJobs() {
  let jobs; try { jobs = await api('/jobs'); } catch { return; }
  JOBS = jobs.filter((j) => j.source.kind !== 'online' && j.source.path);
  const sig = JOBS.map((j) => j.id + j.state).join() + SEL; if (sig === JOBSIG) return; JOBSIG = sig;
  $('#jobcount').textContent = JOBS.length;
  const el = $('#jobs');
  el.innerHTML = JOBS.map((j) => { const ok = ['BLOCKS_COMPLETE', 'INDEXED', 'CONTENT_CANDIDATE'].includes(j.state) || (j.block_counts && Object.keys(j.block_counts).length); return `<div class="li ${ok ? 'clickable' : ''} ${j.id === SEL ? 'sel' : ''}" data-id="${esc(j.id)}" style="${ok ? '' : 'opacity:.5'}"><div class="avatar">${icon('video')}</div><div class="grow"><div class="t ellipsis" title="${attr(j.source.path || '')}">${esc(j.source.name)}</div><div class="d">${j.source.duration_seconds ? ts(j.source.duration_seconds) : '–'} · ${(j.block_counts || {}).quotes ?? 0} quotes · ${(j.block_counts || {}).transcript ?? 0} segs</div></div><span class="tag ${ok ? 'ready' : esc(j.state)}">${ok ? 'ready' : esc(j.state)}</span></div>`; }).join('') || emptyState('video', 'No analysed local videos yet', 'analyse one under Videos');
  el.querySelectorAll('.li.clickable').forEach((n) => n.onclick = () => select(n.dataset.id));
}
async function select(id) {
  if (id === SEL) return; SEL = id; JOB = JOBS.find((x) => x.id === id); CUTS = null; CUT = null; JOBSIG = ''; history.replaceState(null, '', '#composer/' + id); await refreshJobs(); mountEditor();
  try { CUTS = await api('/jobs/' + id + '/cuts'); DUR = CUTS.duration_seconds || JOB.source.duration_seconds || 0; } catch (e) { $('#cuts').innerHTML = `<div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div>`; return; }
  if (CUTS.cuts.length) loadCut(CUTS.cuts[0]); else ED = { ...blankEd(), cut_out: Math.min(30, DUR || 30) };
  renderCuts(); syncTrim(); syncFields(); renderCues(); scheduleFraming(true, 0); await refreshRenders(true);
}
function loadCut(c) { CUT = c.id; ED = { cut_in: c.in_seconds, cut_out: c.out_seconds, captions: c.captions.map((x) => ({ ...x })), captions_enabled: true, lower_third: { name: c.lower_third?.name || '', role: c.lower_third?.role || '' }, title: c.title, cut_id: c.id, ...ED_FRAMING() }; FOCUS_MANUAL = false; }

// ---------------------------------------------------------------- editor
function mountEditor() {
  const j = JOB;
  $('#editor').innerHTML = `<div class="bd" style="padding:20px">
  <div class="row between"><h3 class="title-m ellipsis" style="max-width:70%" title="${attr(j.source.name)}">${esc(j.source.name)}</h3><span class="tag mono">${ts(j.source.duration_seconds || 0)}</span></div>
  <div class="player" style="margin-top:12px"><video id="src" src="/api/v1/jobs/${esc(j.id)}/media" controls preload="metadata" playsinline></video></div>
  <div class="trim" id="trim"><div class="win" id="win"></div><div class="h" id="hin" title="drag: IN"></div><div class="h" id="hout" title="drag: OUT"></div><div class="ph" id="ph"></div><span class="tick" style="left:0">0:00</span><span class="tick" id="tickend" style="left:100%"></span></div>
  <div class="row" style="margin-top:22px"><span class="tc">IN <b id="tin">0:00.0</b> &nbsp;→&nbsp; OUT <b id="tout">0:00.0</b> &nbsp;=&nbsp; <b id="tlen">0.0</b>s</span><span class="muted body-s">playhead <b class="mono" id="phv">0.0</b>s</span><span class="sp"></span>
    <button class="btn sm tonal" id="setin" title="key: I">⟵ IN here</button><button class="btn sm tonal" id="setout" title="key: O">OUT here ⟶</button><button class="btn sm outlined" id="loop" title="key: L">${icon('play', 's')}loop the cut</button></div>
  <div class="muted body-s" style="margin-top:6px">Drag the handles or press <kbd>I</kbd> / <kbd>O</kbd> at the playhead · <kbd>space</kbd> play/pause · <kbd>L</kbd> loop · <kbd>←</kbd>/<kbd>→</kbd> step 0.2 s</div>
  <div id="cutwarn"></div>
  <div class="g2 even" style="margin-top:14px">
    <div><div class="row between"><span class="overline">Proposed cuts</span><span id="refinebox"></span></div><div id="cuts" style="margin-top:8px"><div class="loading"><span class="spin"></span></div></div></div>
    <div><span class="overline">Fine trim (seconds)</span>
      <div class="row" style="margin-top:8px;flex-wrap:nowrap"><input type="number" id="cin" step="0.1" min="0" style="flex:1;width:auto;min-width:0"><span>→</span><input type="number" id="cout" step="0.1" min="0" style="flex:1;width:auto;min-width:0"></div>
      <div class="nudge"><span>IN</span><button class="btn xs tonal" data-n="in" data-d="-1">−1s</button><button class="btn xs tonal" data-n="in" data-d="-0.2">−0.2</button><button class="btn xs tonal" data-n="in" data-d="0.2">+0.2</button><button class="btn xs tonal" data-n="in" data-d="1">+1s</button>
        <span>OUT</span><button class="btn xs tonal" data-n="out" data-d="-1">−1s</button><button class="btn xs tonal" data-n="out" data-d="-0.2">−0.2</button><button class="btn xs tonal" data-n="out" data-d="0.2">+0.2</button><button class="btn xs tonal" data-n="out" data-d="1">+1s</button></div>
      <div class="field" style="margin-top:14px"><label>Lower third (name · role)</label><div class="row" style="flex-wrap:nowrap"><input type="text" id="ltname" placeholder="Name" style="flex:1;width:auto;min-width:0"><input type="text" id="ltrole" placeholder="Role / batch / company" style="flex:1;width:auto;min-width:0"></div></div>
      <div class="field" style="margin-top:10px"><label>Title (label for the render)</label><input type="text" id="title"></div></div>
  </div>
  <div class="row between" style="margin-top:20px"><span class="overline">Framing <span class="tag" id="frfit"></span></span><span class="muted body-s" id="frinfo"></span></div>
  <div class="framing" style="margin-top:8px"><div class="frprev" id="frprev"><img id="frimg" alt="frame at IN"><div class="crop" id="frcrop" hidden></div></div>
    <div><div class="seg" id="fitseg"><label title="fill the clip window for wide sources; letterbox only when cropping would lose too much"><input type="radio" name="fit" value="auto" checked>Auto</label><label title="scale to fill the window and crop the overflow"><input type="radio" name="fit" value="cover">Fill</label><label title="show the whole frame, brand colour around it"><input type="radio" name="fit" value="contain">Fit</label></div>
      <div class="row" style="margin-top:12px"><span class="muted body-s" id="frax">focus</span><input type="range" id="focus" min="0" max="100" value="50" style="flex:1"><button class="btn xs tonal" id="refocus" title="re-run face detection on this cut">${icon('people', 's')}faces</button></div>
      <div class="muted body-s" id="frnote" style="margin-top:8px"></div></div></div>
  <div class="row between" style="margin-top:20px"><label class="check"><input type="checkbox" id="capon" checked> Burn captions <span class="tag" id="cuecount"></span></label><div class="row"><button class="btn xs tonal" id="regen" title="regenerate the cues for the current IN/OUT from the transcript">${icon('refresh', 's')}from transcript</button><button class="btn xs" id="addcue">${icon('add', 's')}cue</button></div></div>
  <div class="muted body-s" style="margin:6px 0 8px">Times are seconds from the cut's IN point. Hindi/English both fine; cues outside the cut are greyed.</div>
  <div id="cues"></div>
  <div class="sticky-bottom"><button class="btn filled" id="render" style="width:100%;height:48px;font-size:15px">Render</button><div class="msg" id="rmsg" style="margin-top:8px"></div></div></div>`;
  bindEditor();
}
function bindEditor() {
  const v = $('#src');
  v.addEventListener('timeupdate', () => { if (!root) return; $('#phv').textContent = v.currentTime.toFixed(1); const d = DUR || v.duration || 0; $('#ph').style.left = (d ? 100 * v.currentTime / d : 0) + '%'; if (LOOP && v.currentTime >= ED.cut_out) v.currentTime = ED.cut_in; });
  v.addEventListener('loadedmetadata', () => { if (!DUR) DUR = v.duration; syncTrim(); });
  $('#setin').onclick = () => setIn(v.currentTime); $('#setout').onclick = () => setOut(v.currentTime); $('#loop').onclick = toggleLoop;
  $('#cin').onchange = () => setIn(+$('#cin').value); $('#cout').onchange = () => setOut(+$('#cout').value);
  $$('[data-n]').forEach((b) => b.onclick = () => { const d = +b.dataset.d; if (b.dataset.n === 'in') { setIn(ED.cut_in + d); v.currentTime = ED.cut_in; } else { setOut(ED.cut_out + d); v.currentTime = Math.max(0, ED.cut_out - 1.5); } });
  $('#ltname').oninput = (e) => ED.lower_third.name = e.target.value; $('#ltrole').oninput = (e) => ED.lower_third.role = e.target.value; $('#title').oninput = (e) => ED.title = e.target.value;
  $('#capon').onchange = (e) => { ED.captions_enabled = e.target.checked; syncRenderButton(); };
  $$('input[name=fit]').forEach((r) => r.onchange = () => { ED.fit = r.value; scheduleFraming(!FOCUS_MANUAL, 0); });
  $('#focus').oninput = (e) => { FOCUS_MANUAL = true; const val = +e.target.value / 100; if (cropAxis() === 'y') ED.focus_y = val; else ED.focus_x = val; drawCrop(); };
  $('#refocus').onclick = () => { FOCUS_MANUAL = false; scheduleFraming(true, 0); };
  $('#addcue').onclick = () => { const last = ED.captions.at(-1)?.end || 0; ED.captions.push({ start: r1(Math.max(0, last)), end: r1(Math.max(0, last) + 2), text: '' }); renderCues(); const i = $('#cues .cue:last-child input[type=text]'); if (i) i.focus(); };
  $('#regen').onclick = async () => { try { ED.captions = await api(`/jobs/${SEL}/captions?cut_in=${ED.cut_in}&cut_out=${ED.cut_out}`); renderCues(); toast('Captions regenerated for ' + ts(ED.cut_in) + ' → ' + ts(ED.cut_out)); } catch (e) { toast(e.message, true); } };
  $('#render').onclick = doRender;
  const bar = $('#trim'); let drag = null;
  const pos = (e) => { const r = bar.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (DUR || v.duration || 0); };
  bar.addEventListener('pointerdown', (e) => { if (e.target.id === 'hin' || e.target.id === 'hout') { drag = e.target.id; bar.setPointerCapture(e.pointerId); } else v.currentTime = pos(e); });
  bar.addEventListener('pointermove', (e) => { if (!drag) return; const t = pos(e); if (drag === 'hin') setIn(Math.min(t, ED.cut_out - 0.5)); else setOut(Math.max(t, ED.cut_in + 0.5)); v.currentTime = drag === 'hin' ? ED.cut_in : Math.max(0, ED.cut_out - 0.1); });
  bar.addEventListener('pointerup', () => { drag = null; }); bar.addEventListener('pointercancel', () => { drag = null; });
}
function toggleLoop() { const v = $('#src'); LOOP = !LOOP; if (LOOP) { v.currentTime = ED.cut_in; v.play(); $('#loop').innerHTML = `${icon('pause', 's')}stop loop`; } else { v.pause(); $('#loop').innerHTML = `${icon('play', 's')}loop the cut`; } }
function setIn(t) { ED.cut_in = r1(Math.max(0, Math.min(t, DUR ? DUR - 0.5 : t))); if (ED.cut_out <= ED.cut_in) ED.cut_out = r1(ED.cut_in + 1); syncTrim(); syncFields(); scheduleFraming(!FOCUS_MANUAL); }
function setOut(t) { ED.cut_out = r1(Math.max(0.5, DUR ? Math.min(t, DUR) : t)); if (ED.cut_in >= ED.cut_out) ED.cut_in = r1(Math.max(0, ED.cut_out - 1)); syncTrim(); syncFields(); scheduleFraming(!FOCUS_MANUAL); }
function syncTrim() {
  if (!$('#win')) return; const d = DUR || 1;
  $('#win').style.left = (100 * ED.cut_in / d) + '%'; $('#win').style.width = (100 * (ED.cut_out - ED.cut_in) / d) + '%'; $('#hin').style.left = (100 * ED.cut_in / d) + '%'; $('#hout').style.left = (100 * ED.cut_out / d) + '%'; $('#tickend').textContent = ts(DUR);
  $('#tin').textContent = ts(ED.cut_in); $('#tout').textContent = ts(ED.cut_out); $('#tlen').textContent = (ED.cut_out - ED.cut_in).toFixed(1);
  const len = ED.cut_out - ED.cut_in, lim = SYSTEM.cuts || {}; $('#cutwarn').innerHTML = len > lim.max ? `<div class="banner warn" style="margin-top:10px">${icon('info')}<div>This cut is ${len.toFixed(0)}s — longer than the ${lim.max}s target for shorts. It will still render.</div></div>` : '';
  $$('#cues .cue').forEach((r) => r.classList.toggle('dim', +r.querySelector('[data-k=start]').value >= len)); syncRenderButton();
}
function syncFields() { $('#cin').value = ED.cut_in; $('#cout').value = ED.cut_out; $('#ltname').value = ED.lower_third.name || ''; $('#ltrole').value = ED.lower_third.role || ''; $('#title').value = ED.title || ''; $('#capon').checked = ED.captions_enabled; }
function syncRenderButton() { const b = $('#render'); if (!b) return; const n = selectedPresets().length; b.textContent = `Render ${n} preset${n === 1 ? '' : 's'} · ${(ED.cut_out - ED.cut_in).toFixed(1)}s · template “${TPL}”${ED.captions_enabled ? '' : ' · no captions'}`; b.disabled = !n; }

// ---------------------------------------------------------------- framing
const framingPreset = () => { const p = selectedPresets(); return p.includes('reels') ? 'reels' : (p[0] || 'reels'); };
function scheduleFraming(detect, delay = 450) { clearTimeout(FR_TIMER); FR_TIMER = setTimeout(() => loadFraming(detect), delay); }
async function loadFraming(detect) {
  if (!SEL || !$('#frprev')) return; const req = ++FR_REQ; $('#frnote').innerHTML = detect ? '<span class="spin"></span> looking for faces in this cut…' : '';
  let fr; try { fr = await api(`/jobs/${SEL}/framing?cut_in=${ED.cut_in}&cut_out=${ED.cut_out}&preset=${framingPreset()}&template=${encodeURIComponent(TPL)}&fit=${ED.fit}&detect=${detect ? 'true' : 'false'}`); } catch (e) { if (req === FR_REQ && $('#frnote')) $('#frnote').textContent = e.message; return; }
  if (req !== FR_REQ || !root) return; FR = fr; if (detect && !FOCUS_MANUAL) { ED.focus_x = fr.focus_x; ED.focus_y = fr.focus_y; } renderFraming();
}
function cropAxis() { if (!FR || !FR.crop) return null; const c = FR.crop; return c.scaled_w > c.w ? 'x' : c.scaled_h > c.h ? 'y' : null; }
function renderFraming() {
  if (!FR || !$('#frimg')) return; const img = $('#frimg'); const at = Math.max(0, ED.cut_in + Math.min(0.5, (ED.cut_out - ED.cut_in) / 2)); const key = SEL + '@' + at.toFixed(1);
  if (key !== FR_IMG_KEY) { FR_IMG_KEY = key; img.src = `/api/v1/jobs/${SEL}/frame?at=${at.toFixed(2)}&width=640`; }
  const ax = cropAxis(), sub = FR.subject || {}; $('#frfit').textContent = FR.fit === 'cover' ? 'fill · crop' : 'fit · letterbox';
  $('#frinfo').textContent = `${FR.source_width}×${FR.source_height} → ${FR.video_rect.w}×${FR.video_rect.h} window of the ${FR.preset} canvas`;
  const sl = $('#focus'); sl.disabled = !ax; $('#refocus').disabled = FR.fit !== 'cover' || !FR.detector; $('#frax').textContent = ax === 'y' ? 'focus ↕' : 'focus ↔';
  if (ax) sl.value = Math.round((ax === 'y' ? ED.focus_y : ED.focus_x) * 100);
  let note = '';
  if (FR.fit !== 'cover') note = 'Whole frame shown; the brand colour fills the rest of the window.';
  else if (!ax) note = 'Source matches the window exactly — nothing to crop.';
  else { const keep = ax === 'x' ? FR.crop.w / FR.crop.scaled_w : FR.crop.h / FR.crop.scaled_h; note = `Keeps ${Math.round(keep * 100)}% of the ${ax === 'x' ? 'width' : 'height'}. `;
    if (!FR.detector) note += 'No face detector on this machine — drag the slider to choose what stays in frame.';
    else if (sub.method === 'faces') note += `Faces found in ${sub.frames_with_faces}/${sub.frames_sampled} sampled frames${FOCUS_MANUAL ? ' (slider moved by you)' : ' — window centred on them'}.`;
    else if (sub.frames_sampled) note += `No faces found in ${sub.frames_sampled} sampled frames — centred; drag the slider if the subject is off to one side.`;
    else note += 'Drag the slider or press faces.'; }
  $('#frnote').textContent = note; drawCrop();
}
function drawCrop() { const box = $('#frcrop'); if (!box) return; if (!FR || FR.fit !== 'cover' || !cropAxis()) { box.hidden = true; return; } const c = FR.crop, x = (c.scaled_w - c.w) * ED.focus_x, y = (c.scaled_h - c.h) * ED.focus_y; box.hidden = false; box.style.left = (100 * x / c.scaled_w) + '%'; box.style.top = (100 * y / c.scaled_h) + '%'; box.style.width = (100 * c.w / c.scaled_w) + '%'; box.style.height = (100 * c.h / c.scaled_h) + '%'; box.dataset.l = `${FR.preset} ${c.w}×${c.h}`; }

// ---------------------------------------------------------------- cuts & cues
function renderCuts() {
  const el = $('#cuts'), rb = $('#refinebox');
  rb.innerHTML = CUTS?.refined ? '<span class="tag gold">AI refined</span>' : `<button class="btn xs" id="refine">${icon('spark', 's')}refine with AI</button>`;
  const rf = $('#refine'); if (rf) rf.onclick = async () => { rf.disabled = true; rf.innerHTML = '<span class="spin"></span> asking Gemini…'; try { CUTS = await api('/jobs/' + SEL + '/cuts?refine=true'); if (CUTS.cuts.length) { loadCut(CUTS.cuts[0]); syncTrim(); syncFields(); renderCues(); $('#src').currentTime = ED.cut_in; } renderCuts(); toast(CUTS.refined ? 'Cuts refined by Gemini' : CUTS.warning || 'no change', !CUTS.refined); } catch (e) { toast(e.message, true); rf.disabled = false; } };
  el.innerHTML = (CUTS?.warning ? `<div class="banner warn" style="margin-bottom:8px">${icon('info')}<div>${esc(CUTS.warning)}</div></div>` : '') + ((CUTS?.cuts || []).map((c) => `<div class="cutcard ${c.id === CUT ? 'sel' : ''}" data-id="${esc(c.id)}"><span class="sc">${esc(c.source)} · ${c.score.toFixed(2)}</span><h4>${esc(c.title)}</h4><div class="m"><span class="mono">${esc(c.in_ts)} → ${esc(c.out_ts)}</span> · ${c.duration.toFixed(0)}s${c.lower_third ? ` · 🎙 ${esc(c.lower_third.name)}${c.lower_third.role ? ', ' + esc(c.lower_third.role) : ''}` : ''}</div><div class="m">${esc(c.reason)}</div></div>`).join('') || '<div class="empty">No proposals for this video — set IN/OUT by hand.</div>');
  el.querySelectorAll('.cutcard').forEach((n) => n.onclick = () => { loadCut(CUTS.cuts.find((c) => c.id === n.dataset.id)); el.querySelectorAll('.cutcard').forEach((x) => x.classList.toggle('sel', x === n)); syncTrim(); syncFields(); renderCues(); $('#src').currentTime = ED.cut_in; scheduleFraming(true, 0); toast('Loaded cut ' + n.dataset.id); });
}
function renderCues() {
  const el = $('#cues'); $('#cuecount').textContent = ED.captions.length + ' cues';
  el.innerHTML = ED.captions.map((c, i) => `<div class="cue" data-i="${i}"><input type="number" step="0.1" min="0" value="${c.start}" data-k="start"><input type="number" step="0.1" min="0" value="${c.end}" data-k="end"><input type="text" value="${attr(c.text)}" data-k="text" placeholder="caption text"><button class="x" title="remove cue">×</button></div>`).join('') || '<div class="empty" style="padding:10px">No cues in this window — press “from transcript”, or add a cue.</div>';
  el.querySelectorAll('.cue').forEach((r) => { const i = +r.dataset.i; r.querySelectorAll('input').forEach((inp) => inp.oninput = () => { ED.captions[i][inp.dataset.k] = inp.type === 'number' ? +inp.value : inp.value; if (inp.dataset.k === 'start') r.classList.toggle('dim', +inp.value >= ED.cut_out - ED.cut_in); }); r.querySelector('.x').onclick = () => { ED.captions.splice(i, 1); renderCues(); }; });
  syncTrim();
}
function msg(t, cls) { const m = $('#rmsg'); if (!m) return; m.textContent = t; m.className = 'msg' + (cls === 1 ? ' err' : cls ? ' ' + cls : ''); }
async function doRender() {
  const presets = selectedPresets(); if (!presets.length) return msg('Tick at least one preset.', 1); if (ED.cut_out <= ED.cut_in) return msg('OUT must be after IN.', 1);
  const caps = ED.captions.filter((c) => c.text.trim() && c.end > c.start).map((c) => ({ start: c.start, end: c.end, text: c.text.trim() }));
  $('#render').disabled = true; msg('Queueing…');
  try { const body = { cut_in: ED.cut_in, cut_out: ED.cut_out, presets, template: TPL, captions: ED.captions_enabled ? caps : [], captions_enabled: ED.captions_enabled, lower_third: ED.lower_third.name.trim() ? { name: ED.lower_third.name.trim(), role: ED.lower_third.role.trim() || null } : null, title: ED.title.trim() || null, cut_id: ED.cut_id, fit: ED.fit, focus_x: ED.focus_x, focus_y: ED.focus_y };
    const r = await post('/jobs/' + SEL + '/compose', body); msg('Queued ' + r.renders.length + ' render(s) — they appear on the right as they finish.', 'ok'); toast('Rendering ' + presets.join(' + ')); await refreshRenders(true); }
  catch (e) { msg(e.message, 1); } finally { const b = $('#render'); if (b) b.disabled = false; }
}

// ---------------------------------------------------------------- renders (diff-based so playing previews are never torn down)
function renderCard(r) { const d = document.createElement('div'); d.className = 'render'; d.dataset.id = r.id; d.innerHTML = `<div class="head"><b title="${attr(r.title || '')}">${esc(r.title || r.cut_id || 'cut')}</b><span class="tag st ${esc(r.status)}">${esc(r.status)}</span></div><div class="k meta"></div><div class="prev" hidden></div><div class="errbox"></div><div class="links"></div>`; updateCard(d, r, true); return d; }
function updateCard(d, r, force) {
  const st = d.querySelector('.st'); if (st.textContent !== r.status || force) {
    st.textContent = r.status; st.className = 'tag st ' + r.status;
    d.querySelector('.meta').innerHTML = `${esc(r.preset)} · ${ts(r.cut_in)} → ${ts(r.cut_out)} · ${esc(r.template)}${r.width ? ` · ${r.width}×${r.height} · ${(r.size_bytes / 1e6).toFixed(1)} MB · ${r.render_seconds}s` : ''}${r.spec.captions?.length && r.spec.captions_enabled ? ` · ${r.spec.captions.length} cues` : ''}${r.spec.lower_third ? ` · 🎙 ${esc(r.spec.lower_third.name)}` : ''}${r.detail?.text_shaping === false ? ' · ⚠ no Devanagari shaping' : ''}`;
    d.querySelector('.errbox').innerHTML = r.error ? `<div class="banner err" style="margin-top:8px">${icon('warn')}<div class="mono">${esc(r.error)}</div></div>` : '';
    const prev = d.querySelector('.prev');
    if (r.status === 'DONE' && !prev.dataset.ready) { prev.hidden = false; prev.dataset.ready = '1'; prev.classList.toggle('wide', r.preset !== 'reels'); prev.innerHTML = `<img src="/api/v1/renders/${esc(r.id)}/poster.jpg" alt=""><div class="play"><span>▶</span></div>`; prev.onclick = () => { const v = document.createElement('video'); v.src = `/api/v1/renders/${r.id}/video`; v.controls = true; v.autoplay = true; v.playsInline = true; v.preload = 'auto'; prev.innerHTML = ''; prev.appendChild(v); prev.onclick = null; v.play().catch(() => {}); }; }
    d.querySelector('.links').innerHTML = r.status === 'DONE' ? `<a class="btn xs tonal" href="/api/v1/renders/${esc(r.id)}/video?download=true">${icon('download', 's')}MP4</a>${r.captions_path ? `<a class="btn xs" href="/api/v1/renders/${esc(r.id)}/captions.srt">SRT</a>` : ''}<span class="k ellipsis" style="max-width:160px" title="${attr(r.output_path)}">${esc((r.output_path || '').split('/').slice(-1)[0])}</span><button class="btn xs" data-del="${esc(r.id)}" style="margin-left:auto">${icon('trash', 's')}</button>` : `<button class="btn xs" data-del="${esc(r.id)}" ${r.status === 'RENDERING' ? 'disabled' : ''} style="margin-left:auto">${icon('trash', 's')}</button>`;
    const dl = d.querySelector('[data-del]'); if (dl) dl.onclick = async () => { if (!(await confirmDialog({ title: 'Delete this render?', body: 'The MP4, poster and captions are removed.', ok: 'Delete', danger: true }))) return; try { await del('/renders/' + r.id); d.remove(); toast('Render deleted'); } catch (e) { toast(e.message, true); } };
  }
}
async function refreshRenders(force) {
  if (!SEL || !$('#renders')) return; let list; try { list = await api('/renders?job_id=' + SEL); } catch { return; }
  const el = $('#renders'); const changed = force || list.length !== RENDERS.length || list.some((r, i) => RENDERS[i]?.id !== r.id || RENDERS[i]?.status !== r.status); RENDERS = list; $('#rcount').textContent = list.length;
  if (!changed) return;
  if (!list.length) { el.innerHTML = '<div class="empty">No renders for this video yet. Pick a cut and press Render.</div>'; return; }
  if (el.querySelector('.empty')) el.innerHTML = '';
  const have = new Map([...el.querySelectorAll('.render')].map((d) => [d.dataset.id, d])); let prev = null;
  for (const r of list) { let d = have.get(r.id); if (d) { updateCard(d, r, false); have.delete(r.id); } else d = renderCard(r); if (prev ? prev.nextSibling !== d : el.firstChild !== d) el.insertBefore(d, prev ? prev.nextSibling : el.firstChild); prev = d; }
  have.forEach((d) => d.remove());
}

// ---------------------------------------------------------------- keyboard + template upload
function onKey(e) {
  if (!SEL || !root || e.target.matches('input,textarea,select')) return; const v = $('#src'); if (!v) return;
  if (e.key === 'i' || e.key === 'I') { setIn(v.currentTime); toast('IN = ' + ts(ED.cut_in)); } else if (e.key === 'o' || e.key === 'O') { setOut(v.currentTime); toast('OUT = ' + ts(ED.cut_out)); }
  else if (e.key === ' ') { e.preventDefault(); v.paused ? v.play() : v.pause(); } else if (e.key === 'l' || e.key === 'L') toggleLoop();
  else if (e.key === 'ArrowLeft') { e.preventDefault(); v.currentTime = Math.max(0, v.currentTime - 0.2); } else if (e.key === 'ArrowRight') { e.preventDefault(); v.currentTime = v.currentTime + 0.2; }
}
async function uploadLayer() {
  const f = $('#upfile').files[0], name = $('#upname').value.trim(); const m = $('#upmsg'); if (!f || !name) { m.textContent = 'Template name and file are required.'; m.className = 'msg err'; return; }
  const fd = new FormData(); fd.append('file', f); fd.append('preset', $('#uppreset').value); fd.append('layer', $('#uplayer').value.trim() || 'frame'); fd.append('x', $('#upx').value); fd.append('y', $('#upy').value); fd.append('z', $('#upz').value); fd.append('replace_all', $('#upreplace').checked);
  m.textContent = 'Uploading…'; m.className = 'msg';
  try { const r = await fetch('/api/v1/composer/templates/' + encodeURIComponent(name) + '/layers', { method: 'POST', body: fd }); const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText); m.textContent = 'Saved layer into template “' + name + '”.'; m.className = 'msg ok'; SYSTEM = await api('/composer/system'); TPL = name; renderTemplates(); syncRenderButton(); } catch (e) { m.textContent = e.message; m.className = 'msg err'; }
}

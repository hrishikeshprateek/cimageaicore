// Reels studio: pick an analysed local video, choose a cut, frame it, caption it, render branded shorts.
import { api, post, del, esc, attr, icon, ts, r1, toast, confirmDialog, emptyState } from '../core.js';
import * as editor from './editor.js';
import { installSplitters } from '../panes.js';

export const title = 'Reels studio';
export const subtitle = 'analysed video → proposed cut → branded short';
let root, ctx, SYSTEM = null, JOBS = [], JOBSIG = '', SEL = null, JOB = null, CUTS = null, CUT = null, DUR = 0, TPL = 'placeholder', RENDERS = [];
const ED_FRAMING = () => ({ fit: 'auto', focus_x: 0.5, focus_y: 0.5 });
let ED = blankEd(), FR = null, FOCUS_MANUAL = false, FR_REQ = 0, FR_TIMER = null, FR_IMG_KEY = '', LOOP = false, keyHandler = null;
let TR = null, SNAP = true, TRACK = false, SNAP_TIMER = null, WANT = null, UNSPLIT = null;   // the measured transcript, and whether edges snap to its sentences
function blankEd() { return { cut_in: 0, cut_out: 0, captions: [], captions_enabled: true, lower_third: { name: '', role: '' }, title: '', cut_id: null, ...ED_FRAMING() }; }
const $ = (s) => root && root.querySelector(s);
const $$ = (s) => root ? [...root.querySelectorAll(s)] : [];

export async function render(el, params, c) {
  root = el; ctx = c; SEL = null; JOB = null; RENDERS = []; JOBSIG = ''; WANT = params[0] || null;
  try { SYSTEM = await api('/composer/system'); } catch (e) { root.innerHTML = `<div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div>`; return; }
  if (!SYSTEM.enabled) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('movie', 'The Reels studio is switched off', 'set COMPOSER_ENABLED=true in .env and restart the API')}</div></div>`; return; }
  TPL = SYSTEM.template;
  root.innerHTML = `<div class="modes" id="modes"><button data-mode="edit" class="${MODE === 'edit' ? 'on' : ''}">${icon('layers', 's')}Timeline editor</button><button data-mode="cut" class="${MODE === 'cut' ? 'on' : ''}">${icon('cut', 's')}Quick single cut</button><span class="muted body-s" id="modehint"></span></div><div id="studiobody"></div>`;
  $$('#modes button').forEach((b) => b.onclick = () => setMode(b.dataset.mode));
  await mountMode();
}

export async function tick() { if (!root || !SYSTEM || !SYSTEM.enabled || MODE === 'edit') return; await refreshJobs(); await refreshRenders(false); }
export function destroy() { if (keyHandler) document.removeEventListener('keydown', keyHandler); keyHandler = null; clearTimeout(FR_TIMER); editor.unmount(); const v = document.getElementById('view'); if (v) v.classList.remove('cut-page'); root = null; SEL = null; MODE = 'edit'; }

let MODE = 'edit';   // the studio opens as an editor; the single-cut trimmer is the other tab
const cutMarkup = () => `<div class="nle cutnle">
  <header class="sbar">
    <div class="segmode"><button data-mode="edit">${icon('layers', 's')}Editor</button><button class="on" data-mode="cut">${icon('cut', 's')}Quick cut</button></div>
    <span class="srcname ellipsis" id="cutname">pick a video</span>
    <span class="state" id="cutdur"></span>
    <span class="sp"></span>
    <span class="outsel"><label>output</label><span class="chips" id="presets"><label class="chip on"><input type="checkbox" value="reels" checked hidden>9:16</label><label class="chip"><input type="checkbox" value="square" hidden>1:1</label><label class="chip"><input type="checkbox" value="landscape" hidden>16:9</label></span></span>
    <span class="outsel"><label>frame</label><select id="tpl"></select></span>
    <button class="btn sm filled" id="render">${icon('movie', 's')}Render</button>
  </header>
  <div class="nle-top">
    <aside class="pane src"><div class="ph"><b>Videos</b><span class="sp"></span><span class="muted body-s" id="jobcount"></span></div>
      <div class="pb flush list" id="jobs"><div class="loading"><span class="spin"></span></div></div></aside>

    <div class="split v" data-split="src" title="drag to resize · double-click to reset"></div>
    <section class="pane mon">
      <div class="stage cutstage"><div class="frame" id="frame"><video id="src" preload="metadata" playsinline></video></div></div>
      <div class="transport">
        <button class="btn sm" id="setin" title="set IN at the playhead (I)">IN</button>
        <button class="btn sm filled" id="loop" title="loop the cut (L)">${icon('play', 's')}</button>
        <button class="btn sm" id="setout" title="set OUT at the playhead (O)">OUT</button>
        <span class="tcbox"><b id="tin">0:00.0</b> <span>→</span> <b id="tout">0:00.0</b> <span>= <b id="tlen">0.0</b>s</span></span>
        <span class="sp"></span><span class="muted body-s">playhead <b class="mono" id="phv">0.0</b>s</span>
        <label class="check" title="every edge lands on a sentence boundary"><input type="checkbox" id="snap" checked> snap</label>
      </div>
      <div class="cutbar">
        <div class="trim" id="trim"><div class="win" id="win"></div><div class="h" id="hin" title="drag: IN"></div><div class="h" id="hout" title="drag: OUT"></div><div class="ph" id="ph"></div><span class="tick" style="left:0">0:00</span><span class="tick" id="tickend" style="left:100%"></span></div>
        <div class="speech" id="speech"></div>
        <div id="cutwarn"></div>
      </div>
    </section>

    <div class="split v" data-split="insp" title="drag to resize · double-click to reset"></div>
    <aside class="pane insp">
      <div class="ph"><b>Inspector</b><span class="sp"></span><span class="muted body-s" id="cuecount"></span></div>
      <div class="pb">
        <details class="sec" open><summary>Cut</summary>
          <div class="row" style="flex-wrap:nowrap;gap:6px"><input type="number" id="cin" step="0.1" min="0" style="flex:1;width:auto;min-width:0"><span class="muted">→</span><input type="number" id="cout" step="0.1" min="0" style="flex:1;width:auto;min-width:0"></div>
          <div class="nudge"><span>IN</span><button class="btn xs tonal" data-n="in" data-d="-1">−1</button><button class="btn xs tonal" data-n="in" data-d="-0.2">−.2</button><button class="btn xs tonal" data-n="in" data-d="0.2">+.2</button><button class="btn xs tonal" data-n="in" data-d="1">+1</button>
            <span>OUT</span><button class="btn xs tonal" data-n="out" data-d="-1">−1</button><button class="btn xs tonal" data-n="out" data-d="-0.2">−.2</button><button class="btn xs tonal" data-n="out" data-d="0.2">+.2</button><button class="btn xs tonal" data-n="out" data-d="1">+1</button></div>
          <div class="field" style="margin-top:10px"><label>Title for the render</label><input type="text" id="title"></div>
          <div id="says" hidden></div>
        </details>
        <details class="sec" open><summary>Proposed cuts <span id="refinebox"></span></summary><div id="cuts"><div class="loading"><span class="spin"></span></div></div></details>
        <details class="sec"><summary>Framing <span class="tag" id="frfit"></span></summary>
          <div class="frprev" id="frprev"><img id="frimg" alt="frame at IN"><div class="crop" id="frcrop" hidden></div></div>
          <div class="seg" id="fitseg" style="margin-top:8px"><label><input type="radio" name="fit" value="auto" checked>Auto</label><label><input type="radio" name="fit" value="cover">Fill</label><label><input type="radio" name="fit" value="contain">Fit</label></div>
          <div class="row" style="margin-top:8px"><span class="muted body-s" id="frax">focus</span><input type="range" id="focus" min="0" max="100" value="50" style="flex:1"><button class="btn xs tonal" id="refocus" title="re-run face detection">${icon('people', 's')}</button></div>
          <label class="check" style="margin-top:8px"><input type="checkbox" id="track"> follow the speaker</label>
          <div class="muted body-s" id="frnote" style="margin-top:8px"></div><div class="muted body-s" id="frinfo" style="margin-top:4px"></div>
        </details>
        <details class="sec"><summary>Captions</summary>
          <div class="row between"><label class="check"><input type="checkbox" id="capon" checked> burn them in</label>
            <div class="row gap4"><button class="btn xs tonal" id="regen" title="rebuild from the transcript">${icon('refresh', 's')}</button><button class="btn xs" id="addcue">${icon('add', 's')}</button></div></div>
          <div id="cues" style="margin-top:8px"></div>
        </details>
        <details class="sec"><summary>Lower third</summary>
          <div class="field"><label>Name</label><input type="text" id="ltname" placeholder="Name"></div>
          <div class="field" style="margin-top:8px"><label>Role</label><input type="text" id="ltrole" placeholder="Role / batch / company"></div>
        </details>
        <details class="sec"><summary>Brand frame</summary>
          <img id="tplprev" class="tplprev" alt="template preview" hidden>
          <div class="field" style="margin-top:8px"><label>Template name</label><input type="text" id="upname" placeholder="cimage"></div>
          <div class="row gap4" style="margin-top:6px"><select id="uppreset"><option value="reels">reels</option><option value="square">square</option><option value="landscape">landscape</option></select><input type="text" id="uplayer" value="frame" style="width:90px"></div>
          <div class="row gap4" style="margin-top:6px"><input type="number" id="upx" value="0" placeholder="x"><input type="number" id="upy" value="0" placeholder="y"><input type="number" id="upz" value="0" placeholder="z"></div>
          <label class="check" style="margin-top:6px"><input type="checkbox" id="upreplace" checked> replace this preset's layers</label>
          <input type="file" id="upfile" accept=".png,.mov,.webm" style="margin-top:6px"><button class="btn xs tonal" id="upgo" style="margin-top:6px">${icon('upload', 's')}Upload</button><div class="msg" id="upmsg"></div>
        </details>
        <div class="msg" id="rmsg"></div>
      </div>
    </aside>
  </div>
  <div class="split h" data-split="tl" title="drag to resize · double-click to reset"></div>
  <div class="nle-tl cutstrip"><div class="tlbar"><b class="lbl">Renders</b><span class="tag" id="rcount"></span><span class="sp"></span></div>
    <div class="strip" id="renders"><div class="empty">No renders yet.</div></div></div>
</div>`;
function setMode(m) {
  if (MODE === m) return;
  MODE = m;
  $$('#modes button').forEach((b) => b.classList.toggle('on', b.dataset.mode === m));
  mountMode();
}

async function mountMode() {
  const body = $('#studiobody'); if (!body) return;
  const hint = $('#modehint'); if (hint) hint.textContent = MODE === 'edit' ? 'the AI fills the timeline in — drag the clips to rearrange it' : 'one window of one video, trimmed by hand';
  editor.unmount();
  if (keyHandler) { document.removeEventListener('keydown', keyHandler); keyHandler = null; }
  const modes = $('#modes'); if (modes) modes.hidden = true;   // both modes carry their own switch in the studio bar
  const v = document.getElementById('view'); if (v) v.classList.remove('cut-page');
  if (MODE === 'edit') {
    body.innerHTML = '';
    if (!JOBS.length) { try { JOBS = (await api('/jobs')).filter((j) => j.source.kind !== 'online' && j.source.path); } catch { /* offline */ } }
    const usable = JOBS.filter((j) => ['BLOCKS_COMPLETE', 'INDEXED', 'CONTENT_CANDIDATE'].includes(j.state));
    JOB = JOBS.find((j) => j.id === (WANT || SEL)) || JOB || usable[0] || JOBS[0] || null;
    SEL = JOB ? JOB.id : null;
    if (JOB) history.replaceState(null, '', '#composer/' + JOB.id);
    return editor.mount(body, { job: JOB, jobs: JOBS, onMode: setMode });
  }
  const view = document.getElementById('view'); if (view) view.classList.add('cut-page');   // same surface as the editor
  body.innerHTML = cutMarkup();
  $$('.cutnle .segmode button').forEach((b) => b.onclick = () => setMode(b.dataset.mode));
  if (UNSPLIT) UNSPLIT();
  UNSPLIT = installSplitters($('.cutnle'), { key: 'cut', onResize: () => { if (SEL) scheduleFraming(false, 400); } });
  JOBSIG = '';
  renderTemplates(); await refreshJobs();
  $$('#presets .chip').forEach((l) => l.onclick = (e) => { e.preventDefault(); const cb = l.querySelector('input'); cb.checked = !cb.checked; l.classList.toggle('on', cb.checked); previewTemplate(); syncRenderButton(); if (SEL) scheduleFraming(false, 0); });
  $('#upgo').onclick = uploadLayer;
  keyHandler = onKey; document.addEventListener('keydown', keyHandler);
  const want = WANT || SEL;
  if (want && JOBS.find((j) => j.id === want)) { const keep = want; SEL = null; select(keep); }
}

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
  if (id === SEL) return; SEL = id; JOB = JOBS.find((x) => x.id === id); CUTS = null; CUT = null; JOBSIG = ''; history.replaceState(null, '', '#composer/' + id); await refreshJobs();
  if (MODE === 'edit') { editor.unmount(); return editor.mount($('#editor'), { job: JOB, jobs: JOBS, template: TPL, preset: selectedPresets()[0] || 'reels' }); }
  mountEditor();
  try { CUTS = await api('/jobs/' + id + '/cuts'); DUR = CUTS.duration_seconds || JOB.source.duration_seconds || 0; } catch (e) { $('#cuts').innerHTML = `<div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div>`; return; }
  if (CUTS.cuts.length) loadCut(CUTS.cuts[0]); else ED = { ...blankEd(), cut_out: Math.min(30, DUR || 30) };
  renderCuts(); syncTrim(); syncFields(); renderCues(); scheduleFraming(true, 0); await refreshRenders(true);
  loadTranscript();
}
function loadCut(c) { CUT = c.id; ED = { cut_in: c.in_seconds, cut_out: c.out_seconds, captions: c.captions.map((x) => ({ ...x })), captions_enabled: true, lower_third: { name: c.lower_third?.name || '', role: c.lower_third?.role || '' }, title: c.title, cut_id: c.id, ...ED_FRAMING() }; FOCUS_MANUAL = false; }

// ---------------------------------------------------------------- editor
function mountEditor() {
  const j = JOB;
  const n = $('#cutname'); if (n) { n.textContent = j.source.name; n.title = j.source.path || ''; }
  const d = $('#cutdur'); if (d) d.textContent = ts(j.source.duration_seconds || 0);
  const v = $('#src'); if (v && !v.src.includes(j.id)) v.src = `/api/v1/jobs/${j.id}/media`;

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
  $('#snap').onchange = (e) => { SNAP = e.target.checked; if (SNAP) snapNow(false); else saysBox(); };
  $('#track').onchange = (e) => { TRACK = e.target.checked; syncRenderButton(); };
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
  bar.addEventListener('pointerup', () => { if (drag) scheduleSnap(); drag = null; }); bar.addEventListener('pointercancel', () => { drag = null; });
}
function toggleLoop() { const v = $('#src'); LOOP = !LOOP; if (LOOP) { v.currentTime = ED.cut_in; v.play(); $('#loop').innerHTML = `${icon('pause', 's')}stop loop`; } else { v.pause(); $('#loop').innerHTML = `${icon('play', 's')}loop the cut`; } }
function setIn(t) { ED.cut_in = r1(Math.max(0, Math.min(t, DUR ? DUR - 0.5 : t))); if (ED.cut_out <= ED.cut_in) ED.cut_out = r1(ED.cut_in + 1); syncTrim(); syncFields(); scheduleFraming(!FOCUS_MANUAL); scheduleSnap(); }
function setOut(t) { ED.cut_out = r1(Math.max(0.5, DUR ? Math.min(t, DUR) : t)); if (ED.cut_in >= ED.cut_out) ED.cut_in = r1(Math.max(0, ED.cut_out - 1)); syncTrim(); syncFields(); scheduleFraming(!FOCUS_MANUAL); scheduleSnap(); }
function syncTrim() {
  if (!$('#win')) return; const d = DUR || 1;
  $('#win').style.left = (100 * ED.cut_in / d) + '%'; $('#win').style.width = (100 * (ED.cut_out - ED.cut_in) / d) + '%'; $('#hin').style.left = (100 * ED.cut_in / d) + '%'; $('#hout').style.left = (100 * ED.cut_out / d) + '%'; $('#tickend').textContent = ts(DUR);
  $('#tin').textContent = ts(ED.cut_in); $('#tout').textContent = ts(ED.cut_out); $('#tlen').textContent = (ED.cut_out - ED.cut_in).toFixed(1);
  const len = ED.cut_out - ED.cut_in, lim = SYSTEM.cuts || {}; $('#cutwarn').innerHTML = len > lim.max ? `<div class="banner warn" style="margin-top:10px">${icon('info')}<div>This cut is ${len.toFixed(0)}s — longer than the ${lim.max}s target for shorts. It will still render.</div></div>` : '';
  $$('#cues .cue').forEach((r) => r.classList.toggle('dim', +r.querySelector('[data-k=start]').value >= len)); syncRenderButton();
  $$('#lane .sent').forEach((n) => { const x = TR && TR.sentences && TR.sentences.find((y) => y.i === +n.dataset.i); if (x) n.classList.toggle('on', x.start >= ED.cut_in - 0.05 && x.end <= ED.cut_out + 0.05); });
}
function syncFields() { $('#cin').value = ED.cut_in; $('#cout').value = ED.cut_out; $('#ltname').value = ED.lower_third.name || ''; $('#ltrole').value = ED.lower_third.role || ''; $('#title').value = ED.title || ''; $('#capon').checked = ED.captions_enabled; }
function syncRenderButton() {
  const b = $('#render'); if (!b) return;
  const n = selectedPresets().length;
  b.innerHTML = `${icon('movie', 's')}Render <b>${(ED.cut_out - ED.cut_in).toFixed(1)}s</b>`;
  b.title = `${n} preset${n === 1 ? '' : 's'} · template “${TPL}”${ED.captions_enabled ? '' : ' · no captions'}${TRACK ? ' · tracked' : ''}`;
  b.disabled = !n;
}

// ---------------------------------------------------------------- the speech lane: cut by sentence, not by guesswork
async function loadTranscript() {
  TR = null; drawSpeech();
  if (!SEL) return;
  let d;
  try { d = await api(`/jobs/${SEL}/transcript`); } catch { return; }
  if (!root || !SEL) return;
  if (!d.ready) {
    TR = { pending: d.status === 'running', error: d.error };
    drawSpeech();
    if (d.status === 'running') setTimeout(() => { if (SEL) loadTranscript(); }, 4000);
    return;
  }
  TR = d; drawSpeech(); saysBox();
}

function drawSpeech() {
  const el = $('#speech'); if (!el) return;
  if (!TR || !TR.sentences) {
    const pending = TR && TR.pending;
    el.innerHTML = `<div class="nospeech">${pending ? '<span class="spin"></span> measuring every word of this video…'
      : TR && TR.error ? `<span class="muted body-s">word timings failed: ${esc(TR.error)}</span>`
      : '<span class="muted body-s">No measured word timings for this video yet — cuts will use the analysis estimate.</span>'}
      ${pending ? '' : `<span class="sp"></span><button class="btn xs tonal" id="measure">${icon('spark', 's')}measure now</button>`}</div>`;
    const m = $('#measure');
    if (m) m.onclick = async () => { m.disabled = true; try { await post(`/jobs/${SEL}/transcript`); TR = { pending: true }; drawSpeech(); setTimeout(() => loadTranscript(), 3000); } catch (e) { toast(e.message, true); m.disabled = false; } };
    return;
  }
  const d = DUR || TR.seconds || 1;
  el.innerHTML = `<div class="lane" id="lane">${TR.sentences.map((x) => {
    const on = x.start >= ED.cut_in - 0.05 && x.end <= ED.cut_out + 0.05;
    return `<span class="sent ${on ? 'on' : ''}${x.ends_open ? ' open' : ''}" data-i="${x.i}" style="left:${100 * x.start / d}%;width:${Math.max(0.5, 100 * (x.end - x.start) / d)}%" title="${attr(ts(x.start) + ' → ' + ts(x.end) + (x.speaker ? '  ' + x.speaker : '') + '\n' + x.text)}"><i>${esc(x.text)}</i></span>`;
  }).join('')}</div>
  <div class="lanefoot"><span class="muted body-s">${TR.sentences.length} sentences measured — click one to cut exactly that, shift-click to extend</span><span class="sp"></span><span class="tag mono">${esc((TR.model || '').replace('whisper:', ''))}</span></div>`;
  $$('#lane .sent').forEach((n) => n.onclick = (e) => {
    const x = TR.sentences.find((y) => y.i === +n.dataset.i); if (!x) return;
    if (e.shiftKey && x.end > ED.cut_in) setOut(x.end);
    else { ED.cut_in = x.start; setOut(x.end); }
    const v = $('#src'); if (v) v.currentTime = ED.cut_in;
    snapNow(true);
  });
}

function scheduleSnap() { if (!SNAP || !TR || !TR.sentences) { saysBox(); return; } clearTimeout(SNAP_TIMER); SNAP_TIMER = setTimeout(() => snapNow(false), 400); }

async function snapNow(quiet) {
  if (!SNAP || !SEL || !TR || !TR.sentences) { saysBox(); return; }
  let d;
  try { d = await api(`/jobs/${SEL}/snap?cut_in=${ED.cut_in}&cut_out=${ED.cut_out}`); } catch { return; }
  if (!root || !d.snapped) return;
  const moved = Math.abs(d.cut_in - ED.cut_in) > 0.01 || Math.abs(d.cut_out - ED.cut_out) > 0.01;
  ED.cut_in = d.cut_in; ED.cut_out = d.cut_out;
  syncTrim(); syncFields(); drawSpeech(); saysBox(d);
  if (moved && !quiet) scheduleFraming(!FOCUS_MANUAL);
}

function saysBox(d) {
  const box = $('#says'); if (!box) return;
  const heard = (TR && TR.sentences || []).filter((x) => x.end > ED.cut_in + 0.05 && x.start < ED.cut_out - 0.05);
  const text = (d && d.text) || heard.map((x) => x.text).join(' ');
  if (!text) { box.hidden = true; return; }
  const open = d ? d.ends_open : !!(heard.at(-1) && heard.at(-1).ends_open);
  box.hidden = false;
  box.innerHTML = `<div class="row"><span class="overline">What this cut says</span><span class="sp"></span><span class="muted body-s">${heard.length} sentence${heard.length === 1 ? '' : 's'}</span>${open ? '<span class="tag warn">ends mid-thought</span>' : '<span class="tag ok">complete</span>'}</div><p>${esc(text)}</p>`;
}

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
  try { const body = { cut_in: ED.cut_in, cut_out: ED.cut_out, presets, template: TPL, captions: ED.captions_enabled ? caps : [], captions_enabled: ED.captions_enabled, lower_third: ED.lower_third.name.trim() ? { name: ED.lower_third.name.trim(), role: ED.lower_third.role.trim() || null } : null, title: ED.title.trim() || null, cut_id: ED.cut_id, fit: ED.fit, focus_x: ED.focus_x, focus_y: ED.focus_y, track_faces: TRACK };
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
    d.querySelector('.links').innerHTML = r.status === 'DONE' ? `<a class="btn xs tonal" href="/api/v1/renders/${esc(r.id)}/video?download=true">${icon('download', 's')}MP4</a><span class="exp"><button class="btn xs" title="open this edit in another editor">${icon('open', 's')}timeline</button><span class="menu"><a href="/api/v1/renders/${esc(r.id)}/export?format=fcpxml">FCPXML · Resolve / Final Cut</a><a href="/api/v1/renders/${esc(r.id)}/export?format=edl">EDL · Premiere, Avid</a><a href="/api/v1/renders/${esc(r.id)}/export?format=srt">SRT · captions</a><a href="/api/v1/renders/${esc(r.id)}/export?format=json">JSON · the raw edit</a></span></span>${r.captions_path ? `<a class="btn xs" href="/api/v1/renders/${esc(r.id)}/captions.srt">SRT</a>` : ''}<span class="k ellipsis" style="max-width:160px" title="${attr(r.output_path)}">${esc((r.output_path || '').split('/').slice(-1)[0])}</span><button class="btn xs" data-del="${esc(r.id)}" style="margin-left:auto">${icon('trash', 's')}</button>` : `<button class="btn xs" data-del="${esc(r.id)}" ${r.status === 'RENDERING' ? 'disabled' : ''} style="margin-left:auto">${icon('trash', 's')}</button>`;
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
  if (!SEL || !root || MODE === 'edit' || (e.target && e.target.matches && e.target.matches('input,textarea,select'))) return; const v = $('#src'); if (!v) return;
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

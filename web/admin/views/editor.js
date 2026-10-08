// The edit studio: sources on the left, the program monitor in the middle, the clip inspector on the right, and a real
// timeline along the bottom. The AI fills the timeline in; everything after that is ordinary editing.
import { api, post, put, del, esc, attr, icon, ts, toast, confirmDialog, emptyState } from '../core.js';

let host = null, TL = null, JOBS = [], JOB = null, SEL = null, SRC = null, SWITCH = null, DRAG_SRC = null;       // SRC: the video open in the sources panel
let PX = 24, HEAD = 0, PLAYING = false, TIMER = null, SAVE_T = null, POLL = null, dirty = false, SNAP = true, keys = null, RESIZE = null, RESIZE_T = null, RO = null;
const $ = (s) => host && host.querySelector(s);
const $$ = (s) => host ? [...host.querySelectorAll(s)] : [];
const clip = (id) => (TL ? TL.clips.find((c) => c.id === id) : null);
const total = () => (TL ? TL.clips.reduce((a, c) => a + c.seconds, 0) : 0);
const tc = (t) => `${Math.floor(Math.max(0, t) / 60)}:${String(Math.floor(Math.max(0, t) % 60)).padStart(2, '0')}.${Math.floor((Math.max(0, t) % 1) * 10)}`;
const starts = () => { let a = 0; return TL.clips.map((c) => { const s = a; a += c.seconds; return s; }); };
const SRC_COLOURS = ['#4f9cf9', '#b388f0', '#5fd0a0', '#e8b55f', '#f0888a', '#66d2e8'];
const colourOf = (jobId) => SRC_COLOURS[Math.max(0, TL.jobs.indexOf(jobId)) % SRC_COLOURS.length];
const clipAt = (t) => { const st = starts(); for (let i = TL.clips.length - 1; i >= 0; i--) if (t >= st[i] - 1e-6) return { i, clip: TL.clips[i], start: st[i] }; return null; };

const page = () => document.getElementById('view');

export async function mount(el, { job, jobs, onMode }) {
  SWITCH = onMode || null;
  if (page()) page().classList.add('studio-page');
  host = el; JOB = job; JOBS = (jobs || []).filter((j) => j.source && j.source.path); SRC = job ? job.id : (JOBS[0] || {}).id;
  SEL = null; HEAD = 0;
  host.innerHTML = `<div class="nle"><div class="nle-loading"><span class="spin"></span> opening the edit…</div></div>`;
  let list = [];
  try { list = await api('/timelines' + (job ? '?job_id=' + encodeURIComponent(job.id) : '')); } catch { /* no store */ }
  try {
    TL = list.length ? await api('/timelines/' + list[0].id)
      : await post('/timelines', { job_id: job ? job.id : null, from_cuts: !!job, title: job ? job.source.name : 'New edit' });
  } catch (e) {
    // the chosen video's file is gone (or it came from a link): start an empty edit and let the sources panel fill it
    try { TL = await post('/timelines', { title: 'New edit' }); toast(e.message, true); }
    catch (err) { host.innerHTML = `<div class="nle"><div class="banner err" style="margin:16px">${icon('warn')}<div>${esc(err.message)}</div></div></div>`; return; }
  }
  draw(); fitZoom(); bindKeys(); pollRenders();
  if (TL.clips.length) { SEL = TL.clips[0].id; drawTimeline(); drawInspector(); }   // open on the first clip, with a picture on the monitor
  seek(0); fitFrame();
  // the stage is the authority on how big the monitor may be, and it changes when a panel folds or the window moves
  const stage = host.querySelector('.stage');
  if (stage && window.ResizeObserver) { RO = new ResizeObserver(() => fitFrame()); RO.observe(stage); }
  RESIZE = () => { clearTimeout(RESIZE_T); RESIZE_T = setTimeout(() => { fitFrame(); fitZoom(); }, 120); };
  window.addEventListener('resize', RESIZE);
}

export function unmount() {
  if (page()) page().classList.remove('studio-page');
  if (RESIZE) window.removeEventListener('resize', RESIZE);
  if (RO) { RO.disconnect(); RO = null; }
  RESIZE = null; clearTimeout(RESIZE_T);
  stop(); clearTimeout(SAVE_T); clearTimeout(POLL);
  if (keys) document.removeEventListener('keydown', keys);
  keys = null; host = null; TL = null; SEL = null;
}

// ---------------------------------------------------------------- shell
function draw() {
  host.innerHTML = `<div class="nle">
    <header class="sbar">
      <div class="segmode"><button class="on" data-sw="edit">${icon('layers', 's')}Editor</button><button data-sw="cut">${icon('cut', 's')}Quick cut</button></div>
      <button class="iconbtn" id="tgsrc" title="sources panel">${icon('grid_view', 's')}</button>
      <input class="etitle" id="tltitle" value="${attr(TL.title)}" placeholder="name this edit">
      <span class="state" id="saved">saved</span>
      <span class="sp"></span>
      <span class="outsel"><label>output</label><select id="preset">${[['reels', '9:16 reel'], ['square', '1:1 feed'], ['landscape', '16:9']].map(([k, l]) => `<option value="${k}" ${TL.preset === k ? 'selected' : ''}>${l}</option>`).join('')}</select></span>
      <span class="exp"><button class="btn sm" title="hand this edit to another editor">${icon('open', 's')}Export</button><span class="menu">
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=fcpxml">FCPXML · Resolve / Final Cut</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=edl">EDL · Premiere, Avid</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=srt">SRT · captions</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=json">JSON · the raw edit</a></span></span>
      <button class="btn sm filled" id="render">${icon('movie', 's')}Render</button>
      <button class="iconbtn" id="tginsp" title="clip panel">${icon('tune', 's')}</button>
    </header>
    <div class="nle-top">
      <aside class="pane src"><div class="ph"><b>Sources</b><span class="sp"></span><span class="muted body-s">${JOBS.length}</span></div><div class="pb" id="srcbody"></div></aside>
      <section class="pane mon">
        <div class="stage"><div class="frame ${TL.preset}" id="frame"><video id="pv" preload="metadata" playsinline></video><div class="burn" id="burn" hidden></div></div></div>
        <div class="transport">
          <button class="btn sm" id="tstart" title="back to the start (Home)">⏮</button>
          <button class="btn sm" id="tprev" title="previous clip">⏪</button>
          <button class="btn sm filled" id="tplay" title="play / pause (space)">${icon('play', 's')}</button>
          <button class="btn sm" id="tnext" title="next clip">⏩</button>
          <span class="tcbox"><b id="tcnow">0:00.0</b> <span>/ <span id="tctot">0:00.0</span></span></span>
          <span class="sp"></span><span class="muted body-s" id="tclip"></span></div>
      </section>
      <aside class="pane insp"><div class="ph"><b>Clip</b><span class="sp"></span><span class="muted body-s" id="inspid"></span></div>
        <div class="pb" id="inspbody"></div>
        <div class="renders" id="renders"></div></aside>
    </div>
    <div class="nle-tl">
      <div class="tlbar">
        <button class="btn xs" id="bsplit" title="split at the playhead (S)">${icon('cut', 's')}Split</button>
        <button class="btn xs" id="bdup" title="duplicate (D)">${icon('layers', 's')}Duplicate</button>
        <button class="btn xs danger" id="bdel" title="remove (⌫)">${icon('trash', 's')}Remove</button>
        <span class="divider"></span>
        <button class="btn xs" id="baicuts" title="add this video's proposed cuts">${icon('spark', 's')}AI cuts</button>
        <label class="check xs" title="edges stick to clip boundaries and sentence ends"><input type="checkbox" id="bsnap" checked> snap</label>
        <span class="sp"></span>
        <span class="muted body-s" id="tlstat"></span>
        <span class="zoomer"><button class="btn xs" id="zout" title="zoom out (−)">−</button><input type="range" id="zoom" min="0" max="100" value="30" title="zoom">
          <button class="btn xs" id="zin" title="zoom in (+)">+</button><button class="btn xs" id="zfit" title="fit the whole edit">fit</button></span>
      </div>
      <div class="tlbody">
        <div class="gutter">
          <div class="gh"></div>
          <div class="trkh v"><b>V1</b><span>video</span></div>
          <div class="trkh t"><b>TXT</b><span>on screen</span></div>
        </div>
        <div class="tlscroll" id="tlscroll"><div class="tlinner" id="tlinner">
          <div class="ruler" id="ruler"></div>
          <div class="trk v" id="trkv"></div>
          <div class="trk t" id="trkt"></div>
          <div class="head" id="head"></div>
          <div class="caret" id="caret" hidden></div>
        </div></div>
      </div>
      <div class="legend" id="legend"></div>
    </div></div>`;

  $$('.segmode button').forEach((b) => b.onclick = () => { if (b.dataset.sw === 'cut' && SWITCH) SWITCH('cut'); });
  const nle = () => host.querySelector('.nle');
  const narrow = () => window.matchMedia('(max-width:1180px)').matches;
  const after = () => setTimeout(() => { fitFrame(); fitZoom(); }, 60);
  $('#tgsrc').onclick = (e) => { nle().classList.toggle(narrow() ? 'show-src' : 'no-src'); if (narrow()) nle().classList.remove('show-insp'); e.currentTarget.classList.toggle('on'); after(); };
  $('#tginsp').onclick = (e) => { nle().classList.toggle(narrow() ? 'show-insp' : 'no-insp'); if (narrow()) nle().classList.remove('show-src'); e.currentTarget.classList.toggle('on'); after(); };
  $('#tltitle').oninput = (e) => { TL.title = e.target.value; save(); };
  $('#preset').onchange = (e) => { TL.preset = e.target.value; const f = $('#frame'); if (f) f.className = 'frame ' + TL.preset; fitFrame(); save(true); };
  $('#render').onclick = renderNow;
  $('#tplay').onclick = toggle;
  $('#tstart').onclick = () => seek(0);
  $('#tprev').onclick = () => { const st = starts(); const prev = [...st].reverse().find((s) => s < HEAD - 0.05); seek(prev ?? 0); };
  $('#tnext').onclick = () => { const next = starts().find((s) => s > HEAD + 0.05); seek(next ?? total()); };
  $('#bsplit').onclick = split; $('#bdup').onclick = duplicate; $('#bdel').onclick = remove;
  $('#baicuts').onclick = aiCuts;
  $('#bsnap').onchange = (e) => { SNAP = e.target.checked; };
  $('#zin').onclick = () => zoom(1.5); $('#zout').onclick = () => zoom(1 / 1.5); $('#zfit').onclick = fitZoom;
  $('#zoom').oninput = (e) => { PX = Math.max(2, Math.min(400, 2 * Math.pow(200, +e.target.value / 100))); drawTimeline(); };
  $('#tlscroll').addEventListener('wheel', (e) => { if (!e.ctrlKey && !e.metaKey) return; e.preventDefault(); zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12); }, { passive: false });
  drawSources(); drawTimeline(); drawInspector();
}

// ---------------------------------------------------------------- sources
async function drawSources() {
  const el = $('#srcbody'); if (!el) return;
  el.innerHTML = JOBS.map((j) => `<div class="srcjob ${j.id === SRC ? 'open' : ''}" data-j="${attr(j.id)}">
      <div class="row"><span class="avatar sm">${icon('video')}</span><div class="grow"><div class="t ellipsis" title="${attr(j.source.name)}">${esc(j.source.name)}</div>
      <div class="muted body-s">${j.source.duration_seconds ? ts(j.source.duration_seconds) : '–'} · ${(j.block_counts || {}).quotes ?? 0} quotes</div></div>${icon('chevron')}</div>
      <div class="srcbody" id="sb-${attr(j.id)}"></div></div>`).join('') || emptyState('video', 'No analysed videos with a local file');
  $$('.srcjob .row').forEach((n) => n.onclick = () => openSource(n.parentElement.dataset.j));
  if (SRC) openSource(SRC, true);
}

async function openSource(id, keep) {
  SRC = (SRC === id && !keep) ? null : id;
  $$('.srcjob').forEach((n) => n.classList.toggle('open', n.dataset.j === SRC));
  if (!SRC) return;
  const box = $('#sb-' + SRC); if (!box) return;
  box.innerHTML = '<div class="loading"><span class="spin"></span></div>';
  const [cuts, tr] = await Promise.all([api('/jobs/' + SRC + '/cuts').catch(() => ({ cuts: [] })), api('/jobs/' + SRC + '/transcript').catch(() => ({ ready: false }))]);
  const cutRows = (cuts.cuts || []).map((c) => `<div class="srcitem" data-a="${c.in_seconds}" data-b="${c.out_seconds}" data-l="${attr(c.title.slice(0, 60))}">
      <span class="tag ${esc(c.source)}">${esc(c.source).replace('_', ' ')}</span><span class="grow ellipsis" title="${attr(c.title)}">${esc(c.title)}</span>
      <span class="muted body-s mono">${c.duration.toFixed(0)}s</span><button class="btn xs tonal">+</button></div>`).join('');
  const sentRows = (tr.ready ? tr.sentences : []).map((s) => `<div class="srcitem sent" data-a="${s.start}" data-b="${s.end}" data-l="${attr(s.text.slice(0, 50))}">
      <span class="mono muted body-s">${ts(s.start)}</span><span class="grow ellipsis" title="${attr(s.text)}">${esc(s.text)}</span>
      <span class="muted body-s mono">${(s.end - s.start).toFixed(1)}s</span><button class="btn xs tonal">+</button></div>`).join('');
  box.innerHTML = `${cutRows ? `<div class="overline">Proposed by the AI</div>${cutRows}` : ''}
    ${sentRows ? `<div class="overline" style="margin-top:10px">Sentences${tr.ready ? '' : ''}</div><div class="sentscroll">${sentRows}</div>`
      : '<div class="muted body-s" style="padding:6px 0">No measured sentences for this video yet.</div>'}`;
  box.querySelectorAll('.srcitem').forEach((n) => {
    n.onclick = () => addClip(SRC, +n.dataset.a, +n.dataset.b, n.dataset.l);
    n.addEventListener('pointerdown', (e) => {          // or drag it onto the timeline, where you want it
      if (e.target.closest('.btn')) return;
      const start = { x: e.clientX, y: e.clientY };
      const move = (ev) => {
        if (DRAG_SRC || Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 6) return;
        DRAG_SRC = { job: SRC, a: +n.dataset.a, b: +n.dataset.b, label: n.dataset.l };
        document.body.classList.add('dragging-src');
      };
      const up = () => {
        document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up);
        setTimeout(() => { if (DRAG_SRC) { DRAG_SRC = null; document.body.classList.remove('dragging-src'); const c = $('#caret'); if (c) c.hidden = true; } }, 30);
      };
      document.addEventListener('pointermove', move); document.addEventListener('pointerup', up);
    });
  });
}

async function addClip(job_id, a, b, label, at) {
  try {
    TL = await post(`/timelines/${TL.id}/clips`, { job_id, in_seconds: a, out_seconds: b, label, at: at ?? null });
    drawTimeline(); fitZoom(); toast('Added to the timeline');
  } catch (e) { toast(e.message, true); }
}

async function aiCuts() {
  if (!SRC) return toast('Open a video in Sources first', true);
  const cuts = await api('/jobs/' + SRC + '/cuts').catch(() => ({ cuts: [] }));
  let n = 0;
  for (const c of cuts.cuts || []) {
    if (TL.clips.some((x) => x.job_id === SRC && Math.abs(x.in_seconds - c.in_seconds) < 0.5)) continue;
    TL = await post(`/timelines/${TL.id}/clips`, { job_id: SRC, in_seconds: c.in_seconds, out_seconds: c.out_seconds, label: c.title.slice(0, 60) });
    n++;
  }
  drawTimeline(); fitZoom();
  toast(n ? `${n} cut${n === 1 ? '' : 's'} added` : 'Already on the timeline');
}

// ---------------------------------------------------------------- the timeline
function zoom(f) { PX = Math.max(2, Math.min(400, PX * f)); syncZoom(); drawTimeline(); }
function syncZoom() { const z = $('#zoom'); if (z) z.value = Math.round(100 * Math.log(PX / 2) / Math.log(200)); }

const RATIO = { reels: 9 / 16, square: 1, landscape: 16 / 9 };
/** The monitor is sized here rather than by CSS: an aspect box with both maxima set overflows its stage. */
function fitFrame() {
  const stage = host && host.querySelector('.stage'), f = $('#frame');
  if (!stage || !f || stage.clientHeight < 40) return;
  const ar = RATIO[TL.preset] || 9 / 16, pad = 26;
  const availW = Math.max(60, stage.clientWidth - pad), availH = Math.max(60, stage.clientHeight - pad);
  let h = availH, w = h * ar;
  if (w > availW) { w = availW; h = w / ar; }
  f.style.width = Math.floor(w) + 'px';
  f.style.height = Math.floor(h) + 'px';
}
function fitZoom() { const w = $('#tlscroll') ? $('#tlscroll').clientWidth - 28 : 900; PX = Math.max(2, w / Math.max(4, total())); syncZoom(); drawTimeline(); }

function drawTimeline() {
  if (!TL || !$('#trkv')) return;
  const secs = total(), w = Math.max(200, secs * PX);
  $('#tlinner').style.width = w + 'px';
  $('#tlstat').textContent = `${TL.clips.length} clips · ${tc(secs)} · ${TL.jobs.length} source${TL.jobs.length === 1 ? '' : 's'}`;
  $('#tctot').textContent = tc(secs);

  const step = PX > 60 ? 1 : PX > 24 ? 5 : PX > 8 ? 10 : 30;
  let marks = '';
  for (let t = 0; t <= secs + 0.01; t += step) marks += `<span style="left:${t * PX}px">${tc(t).replace(/\.\d$/, '')}</span>`;
  $('#ruler').innerHTML = marks;

  const st = starts();
  $('#trkv').innerHTML = TL.clips.map((c, i) => `<div class="cl${c.kind !== 'clip' ? ' still' : ''}${c.id === SEL ? ' sel' : ''}" data-id="${attr(c.id)}" data-i="${i}"
      style="left:${st[i] * PX}px;width:${Math.max(8, c.seconds * PX - 2)}px;--tone:${colourOf(c.job_id)};--thumb:url('/api/v1/jobs/${esc(c.job_id)}/frame?at=${(c.in_seconds + 0.4).toFixed(2)}&width=150')"
      title="${attr((c.label || 'clip') + '\n' + ts(c.in_seconds) + ' → ' + ts(c.out_seconds))}">
      <span class="h l" data-edge="in"></span>
      <span class="body"><b>${esc(c.label || 'clip')}</b><span class="k">${c.seconds.toFixed(1)}s${c.mute ? ' · muted' : ''}${(c.track || []).length ? ' · tracked' : ''}</span></span>
      <span class="h r" data-edge="out"></span></div>`).join('');
  const lg = $('#legend');
  if (lg) lg.innerHTML = TL.jobs.length > 1 ? TL.jobs.map((j) => {
    const name = ((JOBS.find((x) => x.id === j) || {}).source || {}).name || j;
    const n = TL.clips.filter((c) => c.job_id === j).length;
    return `<span class="lg"><i style="background:${colourOf(j)}"></i>${esc(name.slice(0, 26))}<b>${n}</b></span>`;
  }).join('') : '';
  $('#trkt').innerHTML = TL.clips.map((c, i) => (c.text || (c.captions || []).length)
    ? `<div class="txt" data-id="${attr(c.id)}" style="left:${st[i] * PX}px;width:${Math.max(6, c.seconds * PX - 2)}px" title="${attr(c.text || (c.captions[0] || {}).text || '')}">${esc(c.text || (c.captions[0] || {}).text || '')}</div>`
    : '').join('');
  moveHead();
  bindTrack();
}

function bindTrack() {
  const inner = $('#tlinner'), trk = $('#trkv'), caret = $('#caret');
  let mode = null, id = null, startX = 0, base = null, ghost = null, dropAt = null, moved = false;
  const atX = (e) => Math.max(0, (e.clientX - inner.getBoundingClientRect().left) / PX);

  /** Where would a clip dropped at this x land? Returns the index it takes, and draws the caret there. */
  function dropIndex(e, skipId) {
    const st = starts(), x = atX(e);
    let idx = TL.clips.length;
    for (let i = 0; i < TL.clips.length; i++) {
      if (TL.clips[i].id === skipId) continue;
      if (x < st[i] + TL.clips[i].seconds / 2) { idx = i; break; }
    }
    const before = TL.clips[idx];
    const pos = before ? st[TL.clips.indexOf(before)] : starts().reduce((a, _, i, arr) => arr[i] + TL.clips[i].seconds, 0) || total();
    caret.hidden = false;
    caret.style.left = (Math.max(0, before ? pos : total()) * PX) + 'px';
    return idx;
  }
  function endDrag() {
    caret.hidden = true;
    if (ghost) { ghost.remove(); ghost = null; }
    trk.querySelectorAll('.cl').forEach((n) => n.classList.remove('dragging', 'lifted'));
  }

  trk.querySelectorAll('.cl').forEach((n) => {
    n.addEventListener('pointerdown', (e) => {
      const c = clip(n.dataset.id); if (!c) return;
      e.stopPropagation(); e.preventDefault(); select(c.id);
      mode = e.target.dataset.edge ? 'trim:' + e.target.dataset.edge : 'move';
      id = c.id; startX = e.clientX; base = { ...c }; moved = false;
      try { n.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
      n.classList.add('dragging');
    });
    n.addEventListener('pointermove', (e) => {
      if (!mode || id !== n.dataset.id) return;
      const c = clip(id), dt = (e.clientX - startX) / PX;
      if (Math.abs(e.clientX - startX) > 2) moved = true;
      if (mode === 'trim:in') { c.in_seconds = Math.max(0, Math.min(base.in_seconds + dt, c.out_seconds - 0.3)); recompute(); drawTimeline(); }
      else if (mode === 'trim:out') { c.out_seconds = Math.max(c.in_seconds + 0.3, base.out_seconds + dt); recompute(); drawTimeline(); }
      else if (moved) {
        if (!ghost) {     // lift it: a floating copy follows the pointer while the caret shows where it will land
          ghost = n.cloneNode(true);
          ghost.className = 'cl ghost';
          ghost.style.width = n.style.width;
          ghost.style.setProperty('--tone', getComputedStyle(n).getPropertyValue('--tone'));
          ghost.style.setProperty('--thumb', getComputedStyle(n).getPropertyValue('--thumb'));
          inner.appendChild(ghost);
          n.classList.add('lifted');
        }
        ghost.style.left = (atX(e) * PX - (n.offsetWidth / 2)) + 'px';
        dropAt = dropIndex(e, id);
      }
    });
    const finish = () => {
      if (!mode) return;
      const c = clip(id);
      if (mode === 'move' && ghost && dropAt !== null) {
        const from = TL.clips.indexOf(c);
        let to = dropAt > from ? dropAt - 1 : dropAt;
        to = Math.max(0, Math.min(to, TL.clips.length - 1));
        if (to !== from) { TL.clips.splice(from, 1); TL.clips.splice(to, 0, c); }
      }
      mode = null; dropAt = null; endDrag();
      recompute(); drawTimeline();
      if (SNAP && base && (base.in_seconds !== c.in_seconds || base.out_seconds !== c.out_seconds)) snapClip(c);
      save(); drawInspector();
    };
    n.addEventListener('pointerup', finish);
    n.addEventListener('pointercancel', finish);
    n.addEventListener('dblclick', () => { const c = clip(n.dataset.id); if (c) { seek(starts()[TL.clips.indexOf(c)]); play(); } });
  });

  // dropping a source item: the sources panel sets DRAGGING_SRC, the track shows where it would land
  inner.onpointermove = (e) => { if (DRAG_SRC) dropIndex(e, null); };
  inner.onpointerup = async (e) => {
    if (!DRAG_SRC) return;
    const at = dropIndex(e, null);
    const d = DRAG_SRC; DRAG_SRC = null; caret.hidden = true;
    document.body.classList.remove('dragging-src');
    await addClip(d.job, d.a, d.b, d.label, at);
  };

  const scrub = (e) => { seek(Math.min(total(), Math.max(0, atX(e)))); };
  [$('#ruler'), $('#trkv'), $('#trkt')].forEach((lane) => {
    lane.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.cl') || e.target.closest('.txt') || DRAG_SRC) return;
      stop(); scrub(e);
      try { lane.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      lane.dataset.scrub = '1';
    });
    lane.addEventListener('pointermove', (e) => { if (lane.dataset.scrub) scrub(e); });
    const stopScrub = () => delete lane.dataset.scrub;
    lane.addEventListener('pointerup', stopScrub); lane.addEventListener('pointercancel', stopScrub);
  });
}

async function snapClip(c) {
  if (!c || !c.job_id) return;
  try {
    const d = await api(`/jobs/${c.job_id}/snap?cut_in=${c.in_seconds}&cut_out=${c.out_seconds}`);
    if (d.snapped) { c.in_seconds = d.cut_in; c.out_seconds = d.cut_out; recompute(); drawTimeline(); save(); }
  } catch { /* no transcript: leave it where the editor put it */ }
}

function recompute() { TL.clips.forEach((c) => { c.seconds = Math.round((c.out_seconds - c.in_seconds) * 1000) / 1000; }); }

function select(id) {
  SEL = id;
  $$('.cl').forEach((n) => n.classList.toggle('sel', n.dataset.id === id));
  drawInspector();
  const c = clip(id);
  if (c) { const i = TL.clips.indexOf(c); seek(starts()[i] + 0.01, true); }
}

// ---------------------------------------------------------------- playhead + program monitor
function moveHead() {
  const h = $('#head'); if (!h) return;
  h.style.left = (HEAD * PX) + 'px';
  $('#tcnow').textContent = tc(HEAD);
  const at = clipAt(HEAD);
  $('#tclip').textContent = at ? `clip ${at.i + 1}/${TL.clips.length} · ${at.clip.label || ''}`.slice(0, 60) : '';
  const burn = $('#burn'); if (burn) { burn.textContent = at && at.clip.text ? at.clip.text : ''; burn.hidden = !(at && at.clip.text); }
}

function seek(t, keepPlaying) {
  HEAD = Math.max(0, Math.min(total(), t));
  const at = clipAt(HEAD), v = $('#pv');
  if (at && v) {
    const want = `/api/v1/jobs/${at.clip.job_id}/media`;
    const offset = at.clip.in_seconds + (HEAD - at.start);
    if (!v.src.endsWith(want)) { v.src = want; v.addEventListener('loadedmetadata', () => { v.currentTime = offset; }, { once: true }); }
    else v.currentTime = offset;
  }
  if (!keepPlaying) { /* scrubbing stops nothing else */ }
  moveHead();
  const sc = $('#tlscroll');
  if (sc) { const x = HEAD * PX; if (x < sc.scrollLeft + 40 || x > sc.scrollLeft + sc.clientWidth - 60) sc.scrollLeft = Math.max(0, x - sc.clientWidth / 3); }
}

function play() {
  const v = $('#pv'); if (!v || !TL.clips.length) return;
  PLAYING = true; $('#tplay').innerHTML = icon('pause', 's');
  if (HEAD >= total() - 0.05) HEAD = 0;
  seek(HEAD, true);
  v.play().catch(() => { stop(); toast('The browser blocked playback — press play once more', true); });
  clearInterval(TIMER);
  TIMER = setInterval(() => {
    const at = clipAt(HEAD); if (!at) return stop();
    const pos = v.currentTime - at.clip.in_seconds;
    HEAD = at.start + Math.max(0, pos);
    if (pos >= at.clip.seconds - 0.03 || v.ended) {
      const next = at.i + 1;
      if (next >= TL.clips.length) { HEAD = total(); moveHead(); return stop(); }
      HEAD = starts()[next] + 0.001; seek(HEAD, true); v.play().catch(() => {});
      return;
    }
    if (HEAD > total()) return stop();
    moveHead();
  }, 60);
}
function stop() { PLAYING = false; clearInterval(TIMER); TIMER = null; const v = $('#pv'); if (v) v.pause(); const b = $('#tplay'); if (b) b.innerHTML = icon('play', 's'); }
function toggle() { PLAYING ? stop() : play(); }

// ---------------------------------------------------------------- editing operations
function split() {
  const at = clipAt(HEAD); if (!at) return toast('Put the playhead over a clip first', true);
  const offset = HEAD - at.start;
  if (offset < 0.3 || offset > at.clip.seconds - 0.3) return toast('Too close to the edge to split', true);
  const left = at.clip, right = { ...left, id: undefined, in_seconds: left.in_seconds + offset, label: (left.label || 'clip') + ' (b)' };
  delete right.id;
  left.out_seconds = left.in_seconds + offset; left.label = (left.label || 'clip').replace(/ \(b\)$/, '') + ' (a)';
  TL.clips.splice(at.i + 1, 0, right); recompute(); drawTimeline(); save(true); toast('Split');
}
function duplicate() {
  const c = clip(SEL); if (!c) return;
  const copy = { ...c, label: (c.label || 'clip') + ' copy' }; delete copy.id;
  TL.clips.splice(TL.clips.indexOf(c) + 1, 0, copy); recompute(); drawTimeline(); save(true);
}
async function remove() {
  const c = clip(SEL); if (!c) return;
  if (!(await confirmDialog({ title: 'Remove this clip?', body: 'Only from this edit — the video is untouched.', ok: 'Remove', danger: true }))) return;
  TL.clips = TL.clips.filter((x) => x.id !== c.id); SEL = null; recompute(); drawTimeline(); drawInspector(); save(true);
}

function bindKeys() {
  keys = (e) => {
    if (!host || (e.target && e.target.matches && e.target.matches('input,textarea,select,[contenteditable]'))) return;
    const k = e.key.toLowerCase();
    if (k === ' ') { e.preventDefault(); toggle(); }
    else if (k === 's') { e.preventDefault(); split(); }
    else if (k === 'd') { e.preventDefault(); duplicate(); }
    else if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); remove(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); stop(); seek(HEAD - (e.shiftKey ? 1 : 0.2)); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); stop(); seek(HEAD + (e.shiftKey ? 1 : 0.2)); }
    else if (e.key === 'Home') seek(0);
    else if (k === '+' || k === '=') zoom(1.5);
    else if (k === '-') zoom(1 / 1.5);
  };
  document.addEventListener('keydown', keys);
}

// ---------------------------------------------------------------- inspector
function drawInspector() {
  const el = $('#inspbody'); if (!el) return;
  const c = clip(SEL);
  $('#inspid').textContent = c ? `${c.seconds.toFixed(1)}s` : '';
  if (!c) { el.innerHTML = `<div class="muted body-s" style="padding:14px 2px">Select a clip on the timeline.<br><br>Keys: <kbd>space</kbd> play · <kbd>S</kbd> split · <kbd>D</kbd> duplicate · <kbd>⌫</kbd> remove · <kbd>←</kbd>/<kbd>→</kbd> step · <kbd>+</kbd>/<kbd>−</kbd> zoom</div>`; return; }
  const job = JOBS.find((j) => j.id === c.job_id);
  el.innerHTML = `<div class="field"><label>Label</label><input type="text" id="ilabel" value="${attr(c.label || '')}"></div>
    <div class="muted body-s mono" style="margin:6px 0 10px">${esc((job && job.source.name) || c.job_id || 'still')}<br>${ts(c.in_seconds)} → ${ts(c.out_seconds)}</div>
    <div class="field"><label>Text on screen</label><input type="text" id="itext" value="${attr(c.text || '')}" placeholder="a few words"></div>
    <div class="row gap4" style="margin-top:10px"><span class="muted body-s" style="width:28px">IN</span>${[-1, -0.2, 0.2, 1].map((d) => `<button class="btn xs tonal" data-n="in" data-d="${d}">${d > 0 ? '+' : ''}${d}</button>`).join('')}</div>
    <div class="row gap4" style="margin-top:4px"><span class="muted body-s" style="width:28px">OUT</span>${[-1, -0.2, 0.2, 1].map((d) => `<button class="btn xs tonal" data-n="out" data-d="${d}">${d > 0 ? '+' : ''}${d}</button>`).join('')}</div>
    <button class="btn xs" id="isnap" style="margin-top:8px">${icon('spark', 's')}snap to the sentence</button>
    <div class="overline" style="margin-top:16px">Framing</div>
    <div class="seg" id="ifit" style="margin-top:6px">${[['auto', 'Auto'], ['cover', 'Fill'], ['contain', 'Fit']].map(([k, l]) => `<label><input type="radio" name="ifit" value="${k}" ${(c.fit || 'auto') === k ? 'checked' : ''}>${l}</label>`).join('')}</div>
    <div class="row" style="margin-top:8px"><span class="muted body-s">focus</span><input type="range" id="ifocus" min="0" max="100" value="${Math.round((c.focus_x ?? 0.5) * 100)}" style="flex:1"></div>
    <label class="check" style="margin-top:8px"><input type="checkbox" id="itrack" ${(c.track || []).length ? 'checked' : ''}> follow the speaker${(c.track || []).length ? ` <span class="tag ok">${c.track.length}</span>` : ''}</label>
    <div class="overline" style="margin-top:16px">Audio</div>
    <label class="check" style="margin-top:6px"><input type="checkbox" id="imute" ${c.mute ? 'checked' : ''}> mute this clip</label>
    <div class="muted body-s" style="margin-top:10px">${(c.captions || []).length ? `${c.captions.length} caption cue${c.captions.length === 1 ? '' : 's'}` : 'no captions'}</div>`;
  $('#ilabel').oninput = (e) => { c.label = e.target.value; save(); drawTimeline(); };
  $('#itext').oninput = (e) => { c.text = e.target.value || null; save(); drawTimeline(); };
  $$('[data-n]').forEach((b) => b.onclick = () => {
    const d = +b.dataset.d;
    if (b.dataset.n === 'in') c.in_seconds = Math.max(0, Math.min(c.in_seconds + d, c.out_seconds - 0.3));
    else c.out_seconds = Math.max(c.in_seconds + 0.3, c.out_seconds + d);
    recompute(); drawTimeline(); drawInspector(); save();
  });
  $('#isnap').onclick = () => snapClip(c);
  $$('input[name=ifit]').forEach((r) => r.onchange = () => { c.fit = r.value === 'auto' ? null : r.value; save(); });
  $('#ifocus').oninput = (e) => { c.focus_x = +e.target.value / 100; save(); };
  $('#imute').onchange = (e) => { c.mute = e.target.checked; drawTimeline(); save(); };
  $('#itrack').onchange = async (e) => {
    if (!e.target.checked) { c.track = []; drawTimeline(); save(); return; }
    e.target.disabled = true;
    try {
      const d = await post(`/jobs/${c.job_id}/track?cut_in=${c.in_seconds}&cut_out=${c.out_seconds}`);
      c.track = d.keys || [];
      toast(c.track.length ? `Following the speaker · ${c.track.length} keys` : 'The speaker barely moves — a steady frame is better');
      drawTimeline(); drawInspector(); save(true);
    } catch (err) { toast(err.message, true); e.target.checked = false; e.target.disabled = false; }
  };
}

// ---------------------------------------------------------------- saving + rendering
function save(now) {
  clearTimeout(SAVE_T); dirty = true; mark();
  const go = async () => {
    try {
      TL = await put('/timelines/' + TL.id, { title: TL.title, preset: TL.preset, template: TL.template, fit: TL.fit, audio: TL.audio,
                                              clips: TL.clips.map(({ at, tracked, seconds, ...rest }) => rest) });
      dirty = false; mark();
    } catch (e) { toast(e.message, true); }
  };
  return now ? go() : (SAVE_T = setTimeout(go, 700));
}
function mark() { const m = $('#saved'); if (m) { m.textContent = dirty ? 'saving…' : 'saved'; m.className = 'state' + (dirty ? ' busy' : ''); } }

async function renderNow() {
  const b = $('#render'); b.disabled = true;
  try { await save(true); const r = await post(`/timelines/${TL.id}/render`, { preset: TL.preset }); toast(`Rendering ${r.clips} clips · ${Math.round(r.seconds)}s`); pollRenders(); }
  catch (e) { toast(e.message, true); }
  b.disabled = false;
}

async function pollRenders() {
  clearTimeout(POLL);
  if (!TL || !host) return;
  let list = [];
  try { list = (await api('/renders?job_id=' + encodeURIComponent(TL.jobs[0] || ''))).filter((r) => (r.detail || {}).timeline_id === TL.id); } catch { /* ignore */ }
  const el = $('#renders');
  if (el) {
    el.innerHTML = list.length ? `<div class="rhd" id="rfold"><b>Renders</b><span class="sp"></span><span class="muted body-s">${list.length}</span>${icon('chevron', 's')}</div>` + list.slice(0, 4).map((r) => {
      const busy = r.status === 'QUEUED' || r.status === 'RENDERING', p = (r.detail || {}).progress;
      return `<div class="evb"><div class="row"><span class="tag ${r.status === 'DONE' ? 'ok' : r.status === 'FAILED' ? 'err' : 'pending'}">${esc(r.status.toLowerCase())}${p ? ` · ${p.scene}/${p.of}` : ''}</span>
        <span class="muted body-s">${r.duration_seconds ? r.duration_seconds.toFixed(1) + 's' : ''}${r.size_bytes ? ' · ' + (r.size_bytes / 1e6).toFixed(1) + ' MB' : ''}</span><span class="sp"></span>
        ${r.status === 'DONE' ? `<a class="btn xs tonal" href="/api/v1/renders/${esc(r.id)}/video?download=true">${icon('download', 's')}</a>` : ''}</div>
        ${busy ? '<div class="progress" style="margin-top:8px"></div>' : ''}
        ${r.error ? `<div class="banner err" style="margin-top:8px">${icon('warn')}<div class="mono">${esc(r.error)}</div></div>` : ''}
        ${r.status === 'DONE' ? `<video controls preload="none" poster="/api/v1/renders/${esc(r.id)}/poster.jpg" src="/api/v1/renders/${esc(r.id)}/video" style="margin-top:8px;width:100%;max-height:190px;border-radius:6px;background:#000"></video>` : ''}</div>`;
    }).join('') : '';
  }
  const fold = $('#rfold');
  if (fold) fold.onclick = () => el.classList.toggle('folded');
  if (el && list.length && !list.some((r) => r.status === 'QUEUED' || r.status === 'RENDERING') && !el.dataset.touched) { el.classList.add('folded'); el.dataset.touched = '1'; }
  if (list.some((r) => r.status === 'QUEUED' || r.status === 'RENDERING')) { if (el) el.classList.remove('folded'); POLL = setTimeout(pollRenders, 2500); }
}

// The edit itself: a timeline of clips the AI filled in and you rearrange. Lives inside the Reels studio's Edit tab.
import { api, post, put, del, esc, attr, icon, ts, toast, confirmDialog, openDialog, emptyState } from '../core.js';

let host = null, TL = null, SEL = null, JOB = null, JOBS = [], SAVE_T = null, RENDERS = [], POLL = null, PREVIEW = null, dirty = false;
const $ = (s) => host && host.querySelector(s);
const $$ = (s) => host ? [...host.querySelectorAll(s)] : [];
const clip = (id) => (TL ? TL.clips.find((c) => c.id === id) : null);
const total = () => (TL ? TL.clips.reduce((a, c) => a + c.seconds, 0) : 0);
const fmt = (t) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}.${Math.floor((t % 1) * 10)}`;

/** Mount the editor for one job. `jobs` is the studio's list, so clips can come from any analysed video. */
export async function mount(el, { job, jobs, template, preset }) {
  host = el; JOB = job; JOBS = jobs || []; SEL = null; RENDERS = [];
  host.innerHTML = `<div class="bd"><div class="loading"><span class="spin"></span> opening the edit…</div></div>`;
  let list = [];
  try { list = await api('/timelines?job_id=' + encodeURIComponent(job.id)); } catch { /* no store */ }
  if (list.length) await load(list[0].id);
  else await create({ job_id: job.id, from_cuts: true, template, preset });
}

export function unmount() { clearTimeout(SAVE_T); clearTimeout(POLL); stopPreview(); host = null; TL = null; }

async function create(body) {
  try { TL = await post('/timelines', body); draw(); pollRenders(); }
  catch (e) { host.innerHTML = `<div class="bd"><div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div></div>`; }
}
async function load(id) {
  try { TL = await api('/timelines/' + id); draw(); pollRenders(); }
  catch (e) { host.innerHTML = `<div class="bd"><div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div></div>`; }
}

function save(now) {
  clearTimeout(SAVE_T); dirty = true; mark();
  const go = async () => {
    try {
      const body = { title: TL.title, preset: TL.preset, template: TL.template, fit: TL.fit, audio: TL.audio, clips: TL.clips.map(strip) };
      TL = await put('/timelines/' + TL.id, body); dirty = false; mark(); drawTrack(); drawInspector();
    } catch (e) { toast(e.message, true); }
  };
  if (now) return go();
  SAVE_T = setTimeout(go, 700);
}
const strip = (c) => { const { at, tracked, seconds, ...rest } = c; return rest; };   // `at`/`seconds`/`tracked` are derived
function mark() { const m = $('#tlsaved'); if (m) { m.textContent = dirty ? 'saving…' : 'saved'; m.className = 'tag ' + (dirty ? '' : 'ok'); } }

// ---------------------------------------------------------------- layout
function draw() {
  if (!host || !TL) return;
  host.innerHTML = `<div class="bd edit">
    <div class="row between ehead">
      <input class="etitle" id="tltitle" value="${attr(TL.title)}" placeholder="name this edit">
      <span class="tag" id="tlsaved">saved</span><span class="sp"></span>
      <span class="muted body-s"><b id="tlen">${fmt(total())}</b> · ${TL.clips.length} clip${TL.clips.length === 1 ? '' : 's'} · ${TL.jobs.length} video${TL.jobs.length === 1 ? '' : 's'}</span>
    </div>
    <div class="row etools">
      <button class="btn sm tonal" id="aicuts" title="put this video's proposed cuts on the timeline">${icon('spark', 's')}AI cuts</button>
      <button class="btn sm tonal" id="addclip">${icon('add', 's')}Add clip</button>
      <button class="btn sm" id="dupclip" disabled>${icon('layers', 's')}Duplicate</button>
      <button class="btn sm danger" id="delclip" disabled>${icon('trash', 's')}Remove</button>
      <span class="sp"></span>
      <select id="tlpreset" title="output format">${[['reels', 'Reel 9:16'], ['square', 'Square 1:1'], ['landscape', 'YouTube 16:9']].map(([k, l]) => `<option value="${k}" ${TL.preset === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <button class="btn sm filled" id="tlrender">${icon('movie', 's')}Render</button>
      <span class="exp"><button class="btn sm" title="hand this edit to another editor">${icon('open', 's')}Export</button><span class="menu">
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=fcpxml">FCPXML · Resolve / Final Cut</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=edl">EDL · Premiere, Avid</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=srt">SRT · captions</a>
        <a href="/api/v1/timelines/${esc(TL.id)}/export?format=json">JSON · the raw edit</a></span></span>
    </div>
    <div class="eprev"><video id="epv" preload="metadata" playsinline></video>
      <div class="ctl"><button class="btn sm" id="eplay">${icon('play', 's')}Play the edit</button><span class="muted body-s" id="epos">0:00.0 / ${fmt(total())}</span><span class="sp"></span><span class="muted body-s" id="epclip"></span></div></div>
    <div class="ruler" id="ruler"></div>
    <div class="track" id="track"></div>
    <div class="muted body-s" style="margin-top:6px">Drag a clip to reorder · drag its edges to trim · click to edit it below. Everything saves itself.</div>
    <div id="insp"></div>
    <div id="tlrenders" class="tlrenders"></div>
  </div>`;
  $('#tltitle').oninput = (e) => { TL.title = e.target.value; save(); };
  $('#tlpreset').onchange = (e) => { TL.preset = e.target.value; save(true); };
  $('#aicuts').onclick = aiCuts;
  $('#addclip').onclick = addClipDialog;
  $('#delclip').onclick = removeSelected;
  $('#dupclip').onclick = duplicateSelected;
  $('#tlrender').onclick = renderNow;
  $('#eplay').onclick = togglePreview;
  drawTrack(); drawInspector();
}

function drawTrack() {
  const el = $('#track'); if (!el) return;
  const secs = total() || 1;
  if (!TL.clips.length) {
    el.innerHTML = `<div class="emptytrack">${emptyState('cut', 'Nothing on the timeline yet', 'press “AI cuts” to let the system propose the moments, or “Add clip”')}</div>`;
    $('#ruler').innerHTML = ''; return;
  }
  let at = 0;
  el.innerHTML = TL.clips.map((c, i) => {
    const left = 100 * at / secs, w = 100 * c.seconds / secs; at += c.seconds;
    const kind = c.kind === 'clip' ? (c.job_id === (JOB && JOB.id) ? '' : ' other') : ' still';
    return `<div class="cl${kind}${c.id === SEL ? ' sel' : ''}" data-id="${attr(c.id)}" data-i="${i}" style="left:${left}%;width:${w}%" title="${attr((c.label || 'clip') + '\n' + ts(c.in_seconds) + ' → ' + ts(c.out_seconds))}">
      <span class="h l" data-edge="in"></span>
      <span class="body"><b>${esc(c.label || 'clip')}</b><span class="k">${c.seconds.toFixed(1)}s${c.mute ? ' · muted' : ''}${c.track && c.track.length ? ' · tracked' : ''}${c.text ? ' · text' : ''}</span></span>
      <span class="h r" data-edge="out"></span></div>`;
  }).join('') + '<div class="php" id="php" hidden></div>';
  const marks = [];
  for (let t = 0; t <= secs; t += secs > 120 ? 30 : secs > 40 ? 10 : 5) marks.push(`<span style="left:${100 * t / secs}%">${fmt(t).replace(/\.\d$/, '')}</span>`);
  $('#ruler').innerHTML = marks.join('');
  $('#tlen').textContent = fmt(secs);
  bindTrack();
}

// ---------------------------------------------------------------- dragging: reorder and trim
function bindTrack() {
  const track = $('#track'); if (!track) return;
  let mode = null, startX = 0, id = null, from = 0, base = null;
  const secsPerPx = () => (total() || 1) / Math.max(1, track.getBoundingClientRect().width);

  track.querySelectorAll('.cl').forEach((n) => {
    n.addEventListener('pointerdown', (e) => {
      const c = clip(n.dataset.id); if (!c) return;
      select(n.dataset.id);
      mode = e.target.dataset.edge ? 'trim:' + e.target.dataset.edge : 'move';
      id = c.id; startX = e.clientX; from = +n.dataset.i; base = { ...c };
      n.setPointerCapture(e.pointerId); n.classList.add('dragging');
      e.preventDefault();
    });
    n.addEventListener('pointermove', (e) => {
      if (!mode || id !== n.dataset.id) return;
      const dt = (e.clientX - startX) * secsPerPx();
      const c = clip(id); if (!c) return;
      if (mode === 'trim:in') {
        c.in_seconds = Math.max(0, Math.min(base.in_seconds + dt, c.out_seconds - 0.4));
      } else if (mode === 'trim:out') {
        c.out_seconds = Math.max(c.in_seconds + 0.4, base.out_seconds + dt);
      } else {
        const w = track.getBoundingClientRect();
        const x = e.clientX - w.left;
        let acc = 0, target = TL.clips.length - 1;
        for (let i = 0; i < TL.clips.length; i++) { acc += TL.clips[i].seconds; if (x < (acc / (total() || 1)) * w.width) { target = i; break; } }
        if (target !== TL.clips.indexOf(c)) { TL.clips.splice(TL.clips.indexOf(c), 1); TL.clips.splice(target, 0, c); recompute(); drawTrack(); return; }
      }
      recompute(); drawTrack();
    });
    const end = () => { if (!mode) return; mode = null; save(); drawInspector(); };
    n.addEventListener('pointerup', end); n.addEventListener('pointercancel', end);
  });
  track.onclick = (e) => { if (e.target === track) select(null); };
}

function recompute() {
  let at = 0;
  TL.clips.forEach((c) => { c.seconds = Math.round((c.out_seconds - c.in_seconds) * 1000) / 1000; c.at = Math.round(at * 1000) / 1000; at += c.seconds; });
}

function select(id) {
  SEL = id;
  $$('.cl').forEach((n) => n.classList.toggle('sel', n.dataset.id === id));
  const has = !!clip(id);
  ['#delclip', '#dupclip'].forEach((s) => { const b = $(s); if (b) b.disabled = !has; });
  drawInspector();
  const c = clip(id), v = $('#epv');
  if (c && v && c.job_id) { if (!v.src.includes(c.job_id)) v.src = `/api/v1/jobs/${c.job_id}/media`; v.currentTime = c.in_seconds; }
}

// ---------------------------------------------------------------- the clip inspector
function drawInspector() {
  const el = $('#insp'); if (!el) return;
  const c = clip(SEL);
  if (!c) { el.innerHTML = `<div class="inspector empty-i">${TL.clips.length ? 'Click a clip on the timeline to edit it.' : ''}</div>`; return; }
  const job = JOBS.find((j) => j.id === c.job_id);
  el.innerHTML = `<div class="inspector">
    <div class="row between"><span class="overline">${esc(c.label || 'clip')}</span><span class="muted body-s mono">${esc((job && job.source.name) || c.job_id || 'still')} · ${ts(c.in_seconds)} → ${ts(c.out_seconds)}</span></div>
    <div class="g2 even" style="margin-top:10px;gap:16px">
      <div>
        <div class="field"><label>Label</label><input type="text" id="ilabel" value="${attr(c.label || '')}"></div>
        <div class="field" style="margin-top:8px"><label>Text on screen (optional)</label><input type="text" id="itext" value="${attr(c.text || '')}" placeholder="a few words, burned into this clip"></div>
        <div class="row gap4" style="margin-top:10px"><span class="muted body-s">IN</span>
          ${[-1, -0.2, 0.2, 1].map((d) => `<button class="btn xs tonal" data-nudge="in" data-d="${d}">${d > 0 ? '+' : ''}${d}</button>`).join('')}</div>
        <div class="row gap4" style="margin-top:6px"><span class="muted body-s">OUT</span>
          ${[-1, -0.2, 0.2, 1].map((d) => `<button class="btn xs tonal" data-nudge="out" data-d="${d}">${d > 0 ? '+' : ''}${d}</button>`).join('')}</div>
        <div class="row gap4" style="margin-top:10px"><button class="btn xs" id="isnap" title="move both edges onto sentence boundaries">${icon('spark', 's')}snap to speech</button><button class="btn xs" id="iplay">${icon('play', 's')}play this clip</button></div>
      </div>
      <div>
        <div class="seg" id="ifit">${[['auto', 'Auto'], ['cover', 'Fill'], ['contain', 'Fit']].map(([k, l]) => `<label><input type="radio" name="ifit" value="${k}" ${(c.fit || 'auto') === k ? 'checked' : ''}>${l}</label>`).join('')}</div>
        <div class="row" style="margin-top:10px"><span class="muted body-s">focus</span><input type="range" id="ifocus" min="0" max="100" value="${Math.round((c.focus_x ?? 0.5) * 100)}" style="flex:1"></div>
        <label class="check" style="margin-top:8px"><input type="checkbox" id="itrack" ${c.track && c.track.length ? 'checked' : ''}> follow the speaker ${c.track && c.track.length ? `<span class="tag ok">${c.track.length} keys</span>` : ''}</label>
        <label class="check" style="margin-top:8px"><input type="checkbox" id="imute" ${c.mute ? 'checked' : ''}> mute this clip</label>
        <div class="muted body-s" style="margin-top:10px">${c.captions && c.captions.length ? `${c.captions.length} caption cue${c.captions.length === 1 ? '' : 's'} carried from the transcript` : 'no captions on this clip'}</div>
      </div>
    </div></div>`;
  $('#ilabel').oninput = (e) => { c.label = e.target.value; save(); };
  $('#itext').oninput = (e) => { c.text = e.target.value || null; save(); };
  $$('[data-nudge]').forEach((b) => b.onclick = () => {
    const d = +b.dataset.d;
    if (b.dataset.nudge === 'in') c.in_seconds = Math.max(0, Math.min(c.in_seconds + d, c.out_seconds - 0.4));
    else c.out_seconds = Math.max(c.in_seconds + 0.4, c.out_seconds + d);
    recompute(); drawTrack(); drawInspector(); save();
  });
  $('#isnap').onclick = async () => {
    try {
      const d = await api(`/jobs/${c.job_id}/snap?cut_in=${c.in_seconds}&cut_out=${c.out_seconds}`);
      if (d.snapped) { c.in_seconds = d.cut_in; c.out_seconds = d.cut_out; recompute(); drawTrack(); drawInspector(); save(true); toast(d.ends_open ? 'Snapped — but it still ends mid-thought' : 'Snapped to the sentence'); }
      else toast('This video has no measured word timings yet', true);
    } catch (e) { toast(e.message, true); }
  };
  $('#iplay').onclick = () => playRange(c);
  $$('input[name=ifit]').forEach((r) => r.onchange = () => { c.fit = r.value === 'auto' ? null : r.value; save(); });
  $('#ifocus').oninput = (e) => { c.focus_x = +e.target.value / 100; save(); };
  $('#imute').onchange = (e) => { c.mute = e.target.checked; drawTrack(); save(); };
  $('#itrack').onchange = async (e) => {
    if (!e.target.checked) { c.track = []; drawTrack(); save(); return; }
    e.target.disabled = true; toast('Following the speaker across this clip…');
    try {
      const d = await post(`/jobs/${c.job_id}/track?cut_in=${c.in_seconds}&cut_out=${c.out_seconds}`);
      c.track = d.keys || [];
      toast(c.track.length ? `Tracked ${c.track.length} keyframes` : 'The speaker barely moves — a steady frame is better here');
      drawTrack(); drawInspector(); save(true);
    } catch (err) { toast(err.message, true); e.target.checked = false; e.target.disabled = false; }
  };
}

// ---------------------------------------------------------------- clips in and out
async function aiCuts() {
  if (!JOB) return;
  const b = $('#aicuts'); b.disabled = true; b.innerHTML = '<span class="spin"></span> proposing…';
  try {
    const cuts = await api('/jobs/' + JOB.id + '/cuts');
    let added = 0;
    for (const c of cuts.cuts || []) {
      if (TL.clips.some((x) => x.job_id === JOB.id && Math.abs(x.in_seconds - c.in_seconds) < 0.5)) continue;
      TL = await post(`/timelines/${TL.id}/clips`, { job_id: JOB.id, in_seconds: c.in_seconds, out_seconds: c.out_seconds, label: c.title.slice(0, 60) });
      added++;
    }
    drawTrack(); drawInspector();
    toast(added ? `${added} proposed cut${added === 1 ? '' : 's'} added` : 'Those cuts are already on the timeline');
  } catch (e) { toast(e.message, true); }
  b.disabled = false; b.innerHTML = `${icon('spark', 's')}AI cuts`;
}

function addClipDialog() {
  const usable = JOBS.filter((j) => j.source.path);
  const dlg = openDialog(`<div class="dhd"><h3>Add a clip</h3><p>Any analysed video on this machine. Pick the moment, or let the sentences decide it.</p></div>
    <div class="dbd"><div class="field"><label>Video</label><select id="acjob">${usable.map((j) => `<option value="${attr(j.id)}" ${JOB && j.id === JOB.id ? 'selected' : ''}>${esc(j.source.name)}</option>`).join('')}</select></div>
      <div id="acsent" class="acsent"><div class="loading"><span class="spin"></span></div></div>
      <div class="row gap16"><div class="field grow"><label>From (s)</label><input type="number" id="acin" step="0.1" min="0" value="0"></div>
        <div class="field grow"><label>To (s)</label><input type="number" id="acout" step="0.1" min="0" value="8"></div></div>
      <div class="msg" id="acmsg"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="acgo">${icon('add')}Add to the timeline</button></div>`, {
    onOpen(d) {
      const sel = d.querySelector('#acjob');
      const loadSentences = async () => {
        const box = d.querySelector('#acsent');
        box.innerHTML = '<div class="loading"><span class="spin"></span></div>';
        let tr; try { tr = await api(`/jobs/${sel.value}/transcript`); } catch { tr = null; }
        if (!tr || !tr.ready) { box.innerHTML = '<div class="muted body-s">No measured sentences for this video — set the seconds by hand.</div>'; return; }
        box.innerHTML = `<div class="overline">Sentences — click one, shift-click a second to take the span</div><div class="sentlist">${tr.sentences.map((s) => `<button class="sentbtn" data-a="${s.start}" data-b="${s.end}">${ts(s.start)} <span>${esc(s.text.slice(0, 80))}</span></button>`).join('')}</div>`;
        box.querySelectorAll('.sentbtn').forEach((btn) => btn.onclick = (e) => {
          if (e.shiftKey) d.querySelector('#acout').value = btn.dataset.b;
          else { d.querySelector('#acin').value = btn.dataset.a; d.querySelector('#acout').value = btn.dataset.b; }
        });
      };
      sel.onchange = loadSentences; loadSentences();
      d.querySelector('#acgo').onclick = async () => {
        const body = { job_id: sel.value, in_seconds: +d.querySelector('#acin').value, out_seconds: +d.querySelector('#acout').value };
        const m = d.querySelector('#acmsg');
        if (body.out_seconds <= body.in_seconds) { m.textContent = 'The end has to be after the start.'; m.className = 'msg err'; return; }
        try { TL = await post(`/timelines/${TL.id}/clips`, body); d.close(); drawTrack(); drawInspector(); toast('Clip added'); }
        catch (e) { m.textContent = e.message; m.className = 'msg err'; }
      };
    },
  });
  return dlg;
}

async function removeSelected() {
  const c = clip(SEL); if (!c) return;
  if (!(await confirmDialog({ title: 'Remove this clip from the edit?', body: 'The video itself is untouched.', ok: 'Remove', danger: true }))) return;
  TL.clips = TL.clips.filter((x) => x.id !== c.id); SEL = null; recompute(); drawTrack(); drawInspector(); save(true);
}
function duplicateSelected() {
  const c = clip(SEL); if (!c) return;
  const copy = { ...c, id: undefined, label: (c.label || 'clip') + ' (copy)' };
  delete copy.id;
  TL.clips.splice(TL.clips.indexOf(c) + 1, 0, copy); recompute(); drawTrack(); save(true);
}

// ---------------------------------------------------------------- preview: play the edit, clip after clip
function stopPreview() { if (PREVIEW) { clearInterval(PREVIEW); PREVIEW = null; } const v = $('#epv'); if (v) v.pause(); const b = $('#eplay'); if (b) b.innerHTML = `${icon('play', 's')}Play the edit`; }
function playRange(c) {
  const v = $('#epv'); if (!v || !c.job_id) return;
  stopPreview();
  if (!v.src.includes(c.job_id)) v.src = `/api/v1/jobs/${c.job_id}/media`;
  v.currentTime = c.in_seconds; v.play();
  PREVIEW = setInterval(() => { if (v.currentTime >= c.out_seconds) stopPreview(); }, 80);
}
function togglePreview() {
  if (PREVIEW) return stopPreview();
  const v = $('#epv'); if (!v || !TL.clips.length) return;
  let i = 0;
  const start = (n) => {
    const c = TL.clips[n]; if (!c) return stopPreview();
    $('#epclip').textContent = `clip ${n + 1}/${TL.clips.length} · ${esc(c.label || '')}`;
    if (!v.src.includes(c.job_id)) { v.src = `/api/v1/jobs/${c.job_id}/media`; v.load(); }
    const go = () => { v.currentTime = c.in_seconds; v.play().catch(() => {}); };
    if (v.readyState >= 1) go(); else v.addEventListener('loadedmetadata', go, { once: true });
  };
  start(0);
  $('#eplay').innerHTML = `${icon('pause', 's')}Stop`;
  PREVIEW = setInterval(() => {
    const c = TL.clips[i]; if (!c) return stopPreview();
    const done = TL.clips.slice(0, i).reduce((a, x) => a + x.seconds, 0);
    const pos = Math.max(0, Math.min(c.seconds, v.currentTime - c.in_seconds));
    $('#epos').textContent = `${fmt(done + pos)} / ${fmt(total())}`;
    const php = $('#php'); if (php) { php.hidden = false; php.style.left = (100 * (done + pos) / (total() || 1)) + '%'; }
    if (v.currentTime >= c.out_seconds - 0.03) { i++; if (i >= TL.clips.length) return stopPreview(); start(i); }
  }, 60);
}

// ---------------------------------------------------------------- render
async function renderNow() {
  const b = $('#tlrender'); b.disabled = true;
  try { const r = await post(`/timelines/${TL.id}/render`, { preset: TL.preset }); toast(`Rendering ${r.clips} clips · ${Math.round(r.seconds)}s`); pollRenders(true); }
  catch (e) { toast(e.message, true); }
  b.disabled = false;
}
async function pollRenders(force) {
  clearTimeout(POLL);
  if (!TL) return;
  let list = [];
  try { list = (await api('/renders?job_id=' + encodeURIComponent(TL.jobs[0] || ''))).filter((r) => (r.detail || {}).timeline_id === TL.id); } catch { /* ignore */ }
  RENDERS = list;
  const el = $('#tlrenders');
  if (el) {
    el.innerHTML = list.length ? `<div class="overline" style="margin-top:18px">Renders of this edit</div>` + list.map((r) => {
      const busy = r.status === 'QUEUED' || r.status === 'RENDERING';
      const p = (r.detail || {}).progress;
      return `<div class="evb"><div class="row"><span class="tag ${r.status === 'DONE' ? 'ok' : r.status === 'FAILED' ? 'err' : 'pending'}">${esc(r.status.toLowerCase())}${p ? ` · clip ${p.scene}/${p.of}` : ''}</span>
        <b>${esc(r.preset)}</b><span class="muted body-s">${r.duration_seconds ? r.duration_seconds.toFixed(1) + 's' : ''}${r.size_bytes ? ' · ' + (r.size_bytes / 1e6).toFixed(1) + ' MB' : ''}</span><span class="sp"></span>
        ${r.status === 'DONE' ? `<a class="btn xs tonal" href="/api/v1/renders/${esc(r.id)}/video?download=true">${icon('download', 's')}MP4</a>` : ''}</div>
        ${busy ? '<div class="progress" style="margin-top:8px"></div>' : ''}
        ${r.error ? `<div class="banner err" style="margin-top:8px">${icon('warn')}<div class="mono">${esc(r.error)}</div></div>` : ''}
        ${r.status === 'DONE' ? `<video controls preload="none" poster="/api/v1/renders/${esc(r.id)}/poster.jpg" src="/api/v1/renders/${esc(r.id)}/video" style="margin-top:10px;max-height:420px;border-radius:var(--r);background:#000"></video>` : ''}</div>`;
    }).join('') : '';
  }
  if (list.some((r) => r.status === 'QUEUED' || r.status === 'RENDERING')) POLL = setTimeout(() => pollRenders(), 2500);
}

// Script writer: say or type an idea (Hindi or English) -> the knowledge base -> a timed, shootable script.
import { api, post, put, del, esc, attr, icon, ago, num, toast, confirmDialog, openDialog, emptyState } from '../core.js';
import { pickImage } from '../picker.js';

export const title = 'Script writer';
export const subtitle = 'speak or type an idea — the script comes back timed, in Hindi or English, from our own footage';

let root, ctx, opts = null, scripts = [], sel = null, script = null, tab = 'scenes', listSig = '', dirty = false, jobs = [];
let voices = null, templates = null, videos = [], videoT = null, vform = { preset: 'reels', audio: 'voiceover', voice_id: '', template: '' };
let form = { language: 'hi', seconds: 30, style: 'viral_reel', job_id: '', idea: '' };
let rec = null;   // live dictation session
const sigOf = (ss) => JSON.stringify([sel, ss.map((s) => [s.id, s.status, s.version, s.title])]);
const mmss = (t) => `${Math.floor(t / 60)}:${String(Math.round(t % 60)).padStart(2, '0')}`;
const lenLabel = (n) => (n < 120 ? `${n}s` : `${Math.round(n / 60)} min`);

export async function render(el, params, c) {
  root = el; ctx = c; sel = params[0] || sel; script = null; tab = 'scenes'; listSig = '';
  if (!opts) opts = await api('/script-options').catch(() => null);
  if (!jobs.length) jobs = await api('/jobs').then((js) => js.filter((j) => j.block_counts && Object.keys(j.block_counts).length)).catch(() => []);
  await draw(true);
}
export async function tick() {
  if (!root || dirty) return;
  const ss = await api('/scripts').catch(() => null); if (!ss) return;
  const writing = script && script.status === 'generating';
  if (sigOf(ss) === listSig && !writing) return;
  await draw(writing);
}
export function destroy() { stopDictation(); clearTimeout(videoT); root = null; }

async function draw(force) {
  if (!opts || !opts.available) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('movie', 'The script writer needs PostgreSQL', 'set DATABASE_URL and restart')}</div></div>`; return; }
  scripts = await api('/scripts'); listSig = sigOf(scripts);
  if (sel && (force || !script)) { try { script = await api('/scripts/' + sel); } catch { script = null; sel = null; } }
  const li = (s) => `<div class="li clickable ${s.id === sel ? 'sel' : ''}" data-id="${esc(s.id)}"><div class="avatar">${icon(s.spoken ? 'mic' : 'movie')}</div><div class="grow"><div class="t" style="white-space:normal;line-height:19px">${esc(s.title || s.idea)}</div><div class="d"><span class="tag ${esc(s.status)}">${esc(s.status).replace('_', ' ')}</span> ${lenLabel(s.target_seconds)} · ${esc(s.language)} · v${s.version} · ${ago(s.created_at)}</div></div></div>`;
  root.innerHTML = composer() + `<div class="review" style="margin-top:20px"><div class="card"><div class="hd" style="padding-bottom:10px"><h3>Scripts</h3><span class="sp"></span><span class="muted body-s">${scripts.length}</span></div><div class="dlist list" id="sl">${scripts.map(li).join('') || emptyState('movie', 'No scripts yet', 'say what you want and press Write the script')}</div></div>
    <div class="card reader" id="sreader">${sel ? '<div class="loading"><span class="spin"></span></div>' : emptyState('spark', 'Your script appears here', 'the writer only uses what our analysed videos actually contain')}</div></div>`;
  bindComposer();
  root.querySelectorAll('#sl .li').forEach((n) => n.onclick = async () => {
    sel = n.dataset.id; tab = 'scenes'; script = null; dirty = false; history.replaceState(null, '', '#scripts/' + sel);
    root.querySelectorAll('#sl .li').forEach((x) => x.classList.toggle('sel', x === n));
    root.querySelector('#sreader').innerHTML = '<div class="loading"><span class="spin"></span></div>';
    try { script = await api('/scripts/' + sel); } catch (e) { toast(e.message, true); }
    drawScript(); if (window.innerWidth <= 1100) root.querySelector('#sreader').scrollIntoView({ behavior: 'smooth' });
  });
  if (sel) drawScript();
}

// ---------------------------------------------------------------- the idea box
function composer() {
  const chip = (on, k, v, label, title) => `<span class="chip ${on ? 'on' : ''}" data-set="${k}" data-v="${attr(String(v))}" title="${attr(title || '')}">${esc(label)}</span>`;
  const sp = opts.dictation.provider;
  return `<div class="card composer"><div class="bd">
    <div class="idea-wrap">
      <textarea id="idea" placeholder="बोलिए या लिखिए — जैसे: “placement के बारे में 30 सेकंड की reel बनाओ”  ·  or type it in English">${esc(form.idea)}</textarea>
      <button class="mic" id="mic" title="${sp === 'browser' ? 'Dictate (Hindi) — hold a thought and speak' : 'Dictate'}">${icon('mic')}<span class="lbl">Speak</span></button>
    </div>
    <div class="row" style="gap:18px;margin-top:14px;align-items:flex-end;flex-wrap:wrap">
      <div><div class="overline">Language of the script</div><div class="chips" style="margin-top:6px">${opts.languages.map((l) => chip(form.language === l.key, 'language', l.key, l.label)).join('')}</div></div>
      <div><div class="overline">Video length</div><div class="chips" style="margin-top:6px">${opts.lengths.map((l) => chip(form.seconds === l.seconds, 'seconds', l.seconds, l.label, `${l.words} words of voiceover · about ${l.scenes} scenes`)).join('')}</div></div>
      <div class="field" style="min-width:190px"><label>Style</label><select id="style">${opts.styles.map((s) => `<option value="${attr(s.key)}" ${form.style === s.key ? 'selected' : ''} title="${attr(s.about)}">${esc(s.label)}</option>`).join('')}</select></div>
      <div class="field grow" style="min-width:220px"><label>Footage to use</label><select id="job"><option value="">the whole library</option>${jobs.map((j) => `<option value="${esc(j.id)}" ${form.job_id === j.id ? 'selected' : ''}>${esc(j.source.name.slice(0, 60))}</option>`).join('')}</select></div>
      <button class="btn filled" id="write">${icon('spark')}Write the script</button>
    </div>
    <div class="muted body-s" id="cmsg" style="margin-top:10px">${esc(hint())}</div>
  </div></div>`;
}

function hint() {
  const l = (opts.lengths.find((x) => x.seconds === form.seconds) || {});
  return `${lenLabel(form.seconds)} → about ${l.words || '–'} words of voiceover across ${l.scenes || '–'} scenes. Every fact comes from our analysed videos; anything the library cannot back is listed as a gap instead of invented.`;
}

function bindComposer() {
  const ta = root.querySelector('#idea'); if (!ta) return;
  ta.oninput = () => { form.idea = ta.value; };
  root.querySelectorAll('[data-set]').forEach((c) => c.onclick = () => {
    form[c.dataset.set] = c.dataset.set === 'seconds' ? +c.dataset.v : c.dataset.v;
    c.parentElement.querySelectorAll('.chip').forEach((x) => x.classList.toggle('on', x === c));
    root.querySelector('#cmsg').textContent = hint();
  });
  root.querySelector('#style').onchange = (e) => { form.style = e.target.value; };
  root.querySelector('#job').onchange = (e) => { form.job_id = e.target.value; };
  root.querySelector('#write').onclick = write;
  root.querySelector('#mic').onclick = toggleDictation;
  ta.onkeydown = (e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') write(); };
}

async function write() {
  const idea = (root.querySelector('#idea').value || '').trim();
  const msg = root.querySelector('#cmsg');
  if (idea.length < 3) { msg.textContent = 'Say or type what the video should be about first.'; return; }
  const btn = root.querySelector('#write'); btn.disabled = true; msg.textContent = 'Reading the knowledge base and writing…';
  try {
    const s = await post('/scripts', { idea, language: form.language, seconds: form.seconds, style: form.style, job_id: form.job_id || null, spoken: !!form.spoken });
    form.idea = ''; form.spoken = false; sel = s.id; script = s; history.replaceState(null, '', '#scripts/' + sel);
    toast('Writing the script…'); ctx.refresh(); await draw(true);
  } catch (e) { msg.textContent = e.message; toast(e.message, true); btn.disabled = false; }
}

// ---------------------------------------------------------------- dictation (Chrome speech API; a local Whisper endpoint can replace it)
function toggleDictation() {
  if (rec) return stopDictation();
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = root.querySelector('#mic');
  if (!SR) { toast('This browser cannot dictate — use Chrome, or type the idea.', true); return; }
  rec = new SR();
  rec.lang = form.language === 'en' ? 'en-IN' : (opts.dictation.language || 'hi-IN');
  rec.continuous = true; rec.interimResults = true;
  const ta = root.querySelector('#idea'); const base = ta.value ? ta.value.trim() + ' ' : '';
  rec.onresult = (e) => {
    let done = '', live = '';
    for (let i = e.resultIndex; i < e.results.length; i++) (e.results[i].isFinal ? done += e.results[i][0].transcript : live += e.results[i][0].transcript);
    ta.value = (base + (ta.dataset.done = (ta.dataset.done || '') + done) + live).replace(/\s+/g, ' ');
    form.idea = ta.value; form.spoken = true;
  };
  rec.onerror = (e) => { toast('Mic: ' + (e.error === 'not-allowed' ? 'permission denied — allow the microphone for this site' : e.error), true); stopDictation(); };
  rec.onend = () => { if (rec) stopDictation(); };
  ta.dataset.done = '';
  rec.start(); btn.classList.add('on'); btn.querySelector('.lbl').textContent = 'Listening… tap to stop';
  root.querySelector('#cmsg').textContent = `Listening in ${rec.lang} — speak naturally, Hindi and English mixed is fine.`;
}
function stopDictation() {
  if (rec) { const r = rec; rec = null; try { r.stop(); } catch { /* already stopped */ } }
  if (!root) return;
  const btn = root.querySelector('#mic'); if (btn) { btn.classList.remove('on'); btn.querySelector('.lbl').textContent = 'Speak'; }
  const m = root.querySelector('#cmsg'); if (m) m.textContent = hint();
}

// ---------------------------------------------------------------- the script
function drawScript() {
  const el = root.querySelector('#sreader'); const s = script; if (!el) return;
  if (!s) { el.innerHTML = emptyState('spark', 'Your script appears here'); return; }
  if (s.status === 'generating') {
    el.innerHTML = `<div class="bd" style="padding:40px 28px;text-align:center"><div class="loading"><span class="spin" style="margin-right:10px"></span>Reading the knowledge base and writing a ${lenLabel(s.target_seconds)} script…</div><div class="muted body-s" style="margin-top:10px">“${esc(s.idea)}”</div></div>`;
    return;
  }
  const planned = (s.scenes || []).reduce((a, x) => a + (+x.seconds || 0), 0);
  const words = (s.scenes || []).reduce((a, x) => a + (x.voiceover || '').trim().split(/\s+/).filter(Boolean).length, 0);
  const canDecide = ['new', 'in_review'].includes(s.status);
  const gaps = (s.extras && s.extras.evidence_gaps) || [], shots = (s.extras && s.extras.shot_list) || [];
  const vo = (s.extras && s.extras.voiceover) || null;
  const tabs = [['scenes', `Scenes (${(s.scenes || []).length})`], ['video', `Voice & video${videos.length ? ' (' + videos.length + ')' : (vo ? ' ✓' : '')}`],
    ['post', 'Caption & post'], ['idea', 'Idea & evidence'], ['versions', `Versions (${s.version})`]];
  let h = `<div class="rhead"><h2 contenteditable="plaintext-only" id="stitle">${esc(s.title || 'Untitled script')}</h2>
    <div class="meta"><span class="tag ${esc(s.status)}">${esc(s.status).replace('_', ' ')}</span><span>v${s.version}</span><span>·</span><span>${esc({ hi: 'हिंदी', en: 'English', hinglish: 'Hinglish' }[s.language] || s.language)}</span><span>·</span>
      <span title="planned vs asked for">${mmss(planned)} / ${lenLabel(s.target_seconds)}</span><span>·</span><span>${num(words)} words</span><span>·</span><span>${esc(s.style.replace('_', ' '))}</span><span>·</span><span>${esc(s.model || '')} / ${esc(s.prompt_version || '')}</span></div>
    ${s.error ? `<div class="banner err" style="margin-top:12px">${icon('warn')}<div class="mono">${esc(s.error)}</div></div>` : ''}
    ${(s.warnings || []).length ? `<div class="banner warn" style="margin-top:12px">${icon('info')}<div>${s.warnings.map(esc).join('<br>')}</div></div>` : ''}</div>
  <div class="actions">${canDecide ? `<button class="btn ok" data-s="approved">${icon('check')}Approve</button><button class="btn danger" data-s="rejected">Reject</button>` : `<button class="btn outlined" data-s="in_review">Reopen</button>`}
    <button class="btn tonal" id="tele">${icon('eye')}Teleprompter</button><button class="btn" id="regen">${icon('refresh')}Rewrite</button><button class="btn" id="copy">${icon('article')}Copy voiceover</button>
    <a class="btn" href="/api/v1/scripts/${esc(s.id)}/export" download>${icon('download')}Download</a><span class="sp"></span><button class="btn xs danger" id="del" title="delete this script">${icon('trash', 's')}</button></div>
  <div class="tabs" style="padding:0 16px">${tabs.map(([k, l]) => `<span class="tab ${tab === k ? 'on' : ''}" data-t="${k}">${l}</span>`).join('')}</div>`;

  const voiced = new Set(((vo && vo.scenes) || []).map((x) => +x.n));
  if (tab === 'scenes') {
    let t = 0;
    h += `<div class="bd"><div class="field" style="margin-bottom:14px"><label>Hook — the first three seconds</label><input type="text" id="shook" value="${attr(s.hook || '')}"></div>
      <div class="scenes">${(s.scenes || []).map((sc, i) => { const from = t; t += +sc.seconds || 0; return sceneCard(sc, i, from, t, voiced.has(sc.n)); }).join('')}</div>
      <div class="row" style="margin-top:12px"><button class="btn sm tonal" id="addscene">${icon('add', 's')}Add a scene</button></div>
      <div class="field" style="margin-top:16px"><label>Call to action</label><input type="text" id="scta" value="${attr(s.cta || '')}"></div>
      ${shots.length ? `<div class="banner" style="margin-top:14px">${icon('movie')}<div><b>Still to film:</b> ${shots.map(esc).join(' · ')}</div></div>` : ''}
      ${gaps.length ? `<div class="banner warn" style="margin-top:10px">${icon('info')}<div><b>The library cannot back:</b> ${gaps.map(esc).join(' · ')}</div></div>` : ''}
      <div class="row" style="margin-top:16px;gap:10px"><button class="btn filled" id="ssave" disabled>${icon('check')}Save as v${s.version + 1}</button><span class="muted body-s" id="smsg">Edit any line, reorder or swap the b-roll — saving keeps the old version.</span></div></div>`;
  }
  if (tab === 'video') h += voiceVideoTab(s, vo);
  if (tab === 'post') h += `<div class="bd"><div class="field"><label>Caption</label><textarea id="scap" style="min-height:120px">${esc(s.caption || '')}</textarea></div>
      <div class="field" style="margin-top:12px"><label>Hashtags</label><input type="text" id="stags" value="${attr((s.hashtags || []).join(' '))}"></div>
      <div class="kv" style="padding:16px 0 0"><dt>Thumbnail</dt><dd>${esc((s.extras || {}).thumbnail_idea || '–')}</dd><dt>Music</dt><dd>${esc((s.extras || {}).music_mood || '–')}</dd></div>
      <div class="row" style="margin-top:14px"><button class="btn filled" id="ssave2">${icon('check')}Save as v${s.version + 1}</button></div></div>`;
  if (tab === 'idea') {
    const ev = (s.evidence && s.evidence.blocks) || [];
    h += `<div class="bd"><div class="overline">What was asked for${s.spoken ? ' (dictated)' : ''}</div><div class="snippet">${esc(s.idea)}</div>
      <div class="overline" style="margin-top:16px">Evidence the writer could use (${ev.length})</div>
      ${ev.map((b) => `<div class="evb"><span class="ty">${esc(b.block_type)}</span> ${b.timestamp ? `<span class="tag mono">${esc(b.timestamp)}</span> ` : ''}${esc(b.text)}<div class="src">${esc(b.source_name)} · ${esc(b.block_id)}</div></div>`).join('') || '<div class="empty">no evidence stored</div>'}</div>`;
  }
  if (tab === 'versions') h += `<div class="bd" id="svers"><div class="loading"><span class="spin"></span></div></div>`;
  el.innerHTML = h;
  bindScript(el, s);
}

function sceneCard(sc, i, from, to, voiced) {
  const ev = (sc.evidence_ids || []).map((x) => `<span class="tag mono" title="knowledge block behind this line">${esc(x)}</span>`).join(' ');
  const shot = sc.b_roll_image_id ? `<img src="/api/v1/images/${esc(sc.b_roll_image_id)}" alt="" loading="lazy">`
    : `<div class="noshot">${icon('movie')}<span>${sc.b_roll_block_id ? 'from the footage' : 'to be filmed'}</span></div>`;
  return `<div class="scene" data-i="${i}">
    <div class="tc"><b>${mmss(from)}</b><span>→ ${mmss(to)}</span><input type="number" step="0.5" min="0.5" data-k="seconds" value="${attr(String(sc.seconds ?? 0))}" title="seconds"><span class="n">scene ${i + 1}</span></div>
    <div class="broll" data-broll="${i}" title="pick a still or upload a photo for this scene">${shot}<span class="swap">${icon('image', 's')}${sc.b_roll_image_id ? 'Change' : 'Pick'}</span></div>
    <div class="fields">
      <input type="text" data-k="visual" value="${attr(sc.visual || '')}" placeholder="what the camera shows">
      <textarea data-k="voiceover" placeholder="what is said over this shot">${esc(sc.voiceover || '')}</textarea>
      <div class="row gap4"><input type="text" data-k="on_screen_text" value="${attr(sc.on_screen_text || '')}" placeholder="text on screen (optional)" style="max-width:280px">${sc.b_roll_block_id ? `<span class="tag mono" title="the moment in the analysed video this shot comes from">${esc(sc.b_roll_block_id)}</span>` : ''}${ev}</div>
      ${voiced ? `<audio class="vo" controls preload="none" src="/api/v1/scripts/${esc(script.id)}/audio/${sc.n}"></audio>` : ''}
    </div>
    <div class="sbtns"><button class="btn xs" data-mv="-1" title="move up">${icon('chevron', 's')}</button><button class="btn xs" data-mv="1" title="move down">${icon('chevron', 's')}</button><button class="btn xs danger" data-rm="${i}" title="delete scene">${icon('trash', 's')}</button></div>
  </div>`;
}

function readScenes(el) {
  return [...el.querySelectorAll('.scene')].map((row, i) => {
    const base = script.scenes[+row.dataset.i] || {};
    const out = { ...base, n: i + 1 };
    row.querySelectorAll('[data-k]').forEach((inp) => { out[inp.dataset.k] = inp.dataset.k === 'seconds' ? +inp.value || 0 : inp.value; });
    return out;
  });
}

function bindScript(el, s) {
  const mark = () => { dirty = true; const b = el.querySelector('#ssave'); if (b) b.disabled = false; };
  el.querySelectorAll('.tab').forEach((t) => t.onclick = async () => { if (dirty && !(await confirmDialog({ title: 'Discard unsaved changes?', ok: 'Discard', danger: true }))) return; dirty = false; tab = t.dataset.t; drawScript(); });
  el.querySelectorAll('.scene input, .scene textarea, #shook, #scta, #stitle').forEach((n) => n.addEventListener('input', mark));
  el.querySelectorAll('[data-mv]').forEach((b) => b.onclick = () => {
    const row = b.closest('.scene'), i = [...el.querySelectorAll('.scene')].indexOf(row), scenes = readScenes(el), j = i + (+b.dataset.mv);
    if (j < 0 || j >= scenes.length) return;
    [scenes[i], scenes[j]] = [scenes[j], scenes[i]];
    script.scenes = scenes; dirty = true; drawScript();
  });
  el.querySelectorAll('[data-rm]').forEach((b) => b.onclick = async () => {
    if (!(await confirmDialog({ title: 'Delete this scene?', ok: 'Delete', danger: true }))) return;
    const scenes = readScenes(el); scenes.splice(+b.dataset.rm, 1); script.scenes = scenes; dirty = true; drawScript();
  });
  const add = el.querySelector('#addscene'); if (add) add.onclick = () => {
    script.scenes = [...readScenes(el), { n: 0, seconds: 5, visual: '', voiceover: '', on_screen_text: null, b_roll_block_id: null, b_roll_image_id: null, evidence_ids: [] }];
    dirty = true; drawScript();
  };
  el.querySelectorAll('[data-broll]').forEach((b) => b.onclick = () => {
    const i = +b.dataset.broll, scenes = readScenes(el);
    pickImage({ jobId: s.job_id || (s.evidence && s.evidence.job_id), extraIds: (s.evidence && s.evidence.offered_images) || [], currentId: scenes[i].b_roll_image_id,
      heading: `Picture for scene ${i + 1}`, onPick: (img) => { scenes[i].b_roll_image_id = img.id; script.scenes = scenes; dirty = true; drawScript(); toast('Picked — save to keep it'); } });
  });
  const save = async () => {
    const body = { title: el.querySelector('#stitle') ? el.querySelector('#stitle').textContent.trim() : undefined, scenes: readScenes(el) };
    if (el.querySelector('#shook')) body.hook = el.querySelector('#shook').value;
    if (el.querySelector('#scta')) body.cta = el.querySelector('#scta').value;
    if (el.querySelector('#scap')) body.caption = el.querySelector('#scap').value;
    if (el.querySelector('#stags')) body.hashtags = el.querySelector('#stags').value.split(/[\s,]+/).map((x) => x.replace(/^#/, '')).filter(Boolean);
    if (tab === 'post') delete body.scenes;
    try { script = await put('/scripts/' + s.id, body); dirty = false; toast('Saved as v' + script.version); drawScript(); listSig = ''; } catch (e) { toast(e.message, true); }
  };
  [el.querySelector('#ssave'), el.querySelector('#ssave2')].forEach((b) => { if (b) b.onclick = save; });
  el.querySelectorAll('.actions [data-s]').forEach((b) => b.onclick = async () => {
    const st = b.dataset.s;
    if (st === 'rejected' && !(await confirmDialog({ title: 'Reject this script?', body: 'It stays in the list and can be reopened.', ok: 'Reject', danger: true }))) return;
    try { script = await post('/scripts/' + s.id + '/status', { status: st }); toast(st === 'approved' ? 'Approved — ready to shoot' : st === 'rejected' ? 'Rejected' : 'Reopened'); listSig = ''; drawScript(); ctx.refresh(); } catch (e) { toast(e.message, true); }
  });
  const rg = el.querySelector('#regen'); if (rg) rg.onclick = () => rewriteDialog(s);
  const cp = el.querySelector('#copy'); if (cp) cp.onclick = async () => {
    const text = [s.hook, ...(s.scenes || []).map((x) => x.voiceover), s.cta].filter(Boolean).join('\n\n');
    try { await navigator.clipboard.writeText(text); toast('Voiceover copied'); } catch { toast('Copy failed — use Download', true); }
  };
  const tl = el.querySelector('#tele'); if (tl) tl.onclick = () => teleprompter(s);
  const dl = el.querySelector('#del'); if (dl) dl.onclick = async () => {
    if (!(await confirmDialog({ title: 'Delete this script?', body: 'Gone for good, with its versions.', ok: 'Delete', danger: true }))) return;
    try { await del('/scripts/' + s.id); sel = null; script = null; toast('Deleted'); await draw(true); } catch (e) { toast(e.message, true); }
  };
  if (tab === 'video') bindVoiceVideo(el, s);
  if (tab === 'versions') api('/scripts/' + s.id + '/versions').then((vs) => { const v = el.querySelector('#svers'); if (v) v.innerHTML = vs.map((x) => `<div class="evb"><b>v${x.version}</b> · ${esc(x.edited_by)} · ${x.scenes} scenes · ${ago(x.created_at)}<div class="muted body-s">${esc(x.title || '')}</div></div>`).join('') || '<div class="empty">no versions</div>'; });
}

function rewriteDialog(s) {
  openDialog(`<div class="dhd"><h3>Rewrite the script</h3><p>The same idea goes back to the writer. The current version is kept.</p></div>
    <div class="dbd"><div class="snippet">${esc(s.idea)}</div>
      <div class="row gap16"><div class="field grow"><label>Language</label><select id="rl">${opts.languages.map((l) => `<option value="${attr(l.key)}" ${s.language === l.key ? 'selected' : ''}>${esc(l.label)}</option>`).join('')}</select></div>
      <div class="field grow"><label>Length</label><select id="rs">${opts.lengths.map((l) => `<option value="${l.seconds}" ${s.target_seconds === l.seconds ? 'selected' : ''}>${esc(l.label)} · ~${l.words} words</option>`).join('')}</select></div>
      <div class="field grow"><label>Style</label><select id="rt">${opts.styles.map((x) => `<option value="${attr(x.key)}" ${s.style === x.key ? 'selected' : ''}>${esc(x.label)}</option>`).join('')}</select></div></div>
      <div class="msg" id="rm"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="rgo">${icon('refresh')}Rewrite</button></div>`, {
    onOpen(dl) {
      dl.querySelector('#rgo').onclick = async () => {
        dl.querySelector('#rgo').disabled = true;
        const q = `?language=${dl.querySelector('#rl').value}&seconds=${dl.querySelector('#rs').value}&style=${dl.querySelector('#rt').value}`;
        try { script = await post('/scripts/' + s.id + '/regenerate' + q); dl.close(); toast('Rewriting…'); drawScript(); }
        catch (e) { const m = dl.querySelector('#rm'); m.textContent = e.message; m.className = 'msg err'; dl.querySelector('#rgo').disabled = false; }
      };
    },
  });
}

// ---------------------------------------------------------------- teleprompter: what the presenter reads, scrolling at speaking pace
function teleprompter(s) {
  const lines = [s.hook, ...(s.scenes || []).map((x) => x.voiceover), s.cta].map((x) => (x || '').trim()).filter(Boolean);
  const el = document.createElement('div');
  el.className = 'tele';
  el.innerHTML = `<div class="tbar"><button class="btn sm" data-t="play">${icon('play')}Start</button><button class="btn sm" data-t="slower">A−</button><button class="btn sm" data-t="faster">A+</button><span class="muted">${lenLabel(s.target_seconds)} · ${lines.length} lines · space = start/stop</span><span class="sp"></span><button class="btn sm" data-t="close">${icon('close')}Close</button></div>
    <div class="tscroll"><div class="tinner">${lines.map((l) => `<p>${esc(l)}</p>`).join('')}<div style="height:60vh"></div></div></div>`;
  document.body.appendChild(el);
  const scroll = el.querySelector('.tscroll'), inner = el.querySelector('.tinner');
  let running = false, size = 44, last = 0, raf = 0, pos = 0;
  // the whole script should pass the camera in the length of the video; keep our own float position, because
  // `scrollTop += 0.4` reads back rounded and never moves
  const pps = () => Math.max(6, (inner.scrollHeight - scroll.clientHeight) / Math.max(8, s.target_seconds));
  const step = (t) => {
    if (!running) return;
    if (last) { pos = Math.min(pos + pps() * (t - last) / 1000, inner.scrollHeight - scroll.clientHeight); scroll.scrollTop = pos; }
    last = t; raf = requestAnimationFrame(step);
  };
  const play = () => { running = !running; last = 0; pos = scroll.scrollTop; el.querySelector('[data-t="play"]').innerHTML = running ? '⏸ Pause' : '▶ Start'; if (running) raf = requestAnimationFrame(step); else cancelAnimationFrame(raf); };
  const close = () => { running = false; cancelAnimationFrame(raf); document.removeEventListener('keydown', key); el.remove(); };
  const key = (e) => { if (e.key === 'Escape') close(); if (e.key === ' ') { e.preventDefault(); play(); } };
  el.querySelectorAll('[data-t]').forEach((b) => b.onclick = () => ({ play, close, slower: () => inner.style.fontSize = (size = Math.max(22, size - 6)) + 'px', faster: () => inner.style.fontSize = (size = Math.min(88, size + 6)) + 'px' }[b.dataset.t]()));
  document.addEventListener('keydown', key);
  inner.style.fontSize = size + 'px';
}


// ---------------------------------------------------------------- voice (ElevenLabs) + the finished video
function voiceVideoTab(s, vo) {
  const prov = opts.voiceover && opts.voiceover.provider;
  const mins = (n) => `${Math.floor(n / 60)}:${String(Math.round(n % 60)).padStart(2, '0')}`;
  const voiceBox = !prov
    ? `<div class="banner warn">${icon('info')}<div><b>Voiceovers are off.</b> Put your ElevenLabs key in <code>.env</code> — <code>TTS_PROVIDER=elevenlabs</code> and <code>ELEVENLABS_API_KEY=…</code> — then restart. The key you already use elsewhere works; nothing else changes.</div></div>`
    : `<div class="row" style="gap:12px;align-items:flex-end;flex-wrap:wrap">
        <div class="field" style="min-width:260px"><label>Voice (${esc(prov)})</label><select id="voice">${(voices ? voices.voices : []).map((v) => `<option value="${attr(v.id)}" ${vform.voice_id === v.id ? 'selected' : ''}>${esc(v.name)}${v.languages && v.languages.length ? ' · ' + esc(v.languages.slice(0, 3).join('/')) : ''}</option>`).join('') || '<option value="">loading…</option>'}</select></div>
        <button class="btn filled" id="speak">${icon('mic')}${vo ? 'Speak it again' : 'Speak the script'}</button>
        <label class="check"><input type="checkbox" id="fitscenes" checked> stretch a scene if its line is longer</label>
        <span class="muted body-s grow" id="vmsg">${vo ? `${vo.scenes.length} lines · ${mins(vo.seconds)} of audio · ${num(vo.chars)} characters used` : 'Each scene is spoken separately, so the voice stays inside its own shot.'}</span>
        ${voices && voices.usage && voices.usage.limit ? `<span class="tag" title="characters used on this ElevenLabs key">${num(voices.usage.used)} / ${num(voices.usage.limit)}</span>` : ''}</div>
      ${(vo && vo.warnings && vo.warnings.length) ? `<div class="banner warn" style="margin-top:12px">${icon('info')}<div>${vo.warnings.map(esc).join('<br>')}</div></div>` : ''}
      ${vo ? `<audio controls preload="none" style="width:100%;margin-top:12px" src="/api/v1/scripts/${esc(s.id)}/audio/${vo.scenes[0].n}" title="scene 1"></audio>` : ''}`;

  const withFootage = (s.scenes || []).filter((x) => x.b_roll_block_id || x.b_roll_image_id).length;
  const videoBox = !opts.composer
    ? `<div class="banner warn">${icon('info')}<div>The video composer is off — set <code>COMPOSER_ENABLED=true</code> to cut the script together here.</div></div>`
    : `<div class="row" style="gap:12px;align-items:flex-end;flex-wrap:wrap">
        <div class="field" style="min-width:150px"><label>Format</label><select id="vpreset">${[['reels', 'Reel 9:16'], ['square', 'Square 1:1'], ['landscape', 'YouTube 16:9']].map(([k, l]) => `<option value="${k}" ${vform.preset === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        <div class="field" style="min-width:170px"><label>Template</label><select id="vtpl">${(templates || []).map((t) => `<option value="${attr(t.name || t)}" ${vform.template === (t.name || t) ? 'selected' : ''}>${esc(t.name || t)}</option>`).join('') || '<option value="">the default</option>'}</select></div>
        <div class="field" style="min-width:210px"><label>Sound</label><select id="vaudio">${[['voiceover', 'the voiceover only'], ['both', 'voiceover over the original sound'], ['source', "the footage's own sound"]].map(([k, l]) => `<option value="${k}" ${vform.audio === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
        <button class="btn filled" id="makevid" ${(!vo && vform.audio !== 'source') ? 'disabled title="speak the script first, or choose the footage&apos;s own sound"' : ''}>${icon('movie')}Make the video</button>
        <span class="muted body-s grow">${withFootage} of ${(s.scenes || []).length} scenes have footage or a still; the rest become a slate you can reshoot later. Rendered with the branded template, like a reel.</span></div>
      <div id="vlist" style="margin-top:14px">${videoList()}</div>`;

  return `<div class="bd"><div class="overline">Voiceover</div><div style="margin-top:10px">${voiceBox}</div>
    <div class="overline" style="margin-top:26px">The video</div><div style="margin-top:10px">${videoBox}</div></div>`;
}

function videoList() {
  if (!videos.length) return '<div class="muted body-s">No video yet.</div>';
  return videos.map((v) => {
    const busy = v.status === 'QUEUED' || v.status === 'RENDERING';
    const prog = v.progress ? ` · scene ${v.progress.scene} of ${v.progress.of}` : '';
    return `<div class="evb"><div class="row"><span class="tag ${v.status === 'DONE' ? 'ok' : v.status === 'FAILED' ? 'err' : 'pending'}">${esc(v.status.toLowerCase())}${esc(prog)}</span>
      <b>${esc(v.preset)}</b><span class="muted body-s">${v.duration ? v.duration.toFixed(1) + 's' : ''} ${v.size_bytes ? '· ' + (v.size_bytes / 1e6).toFixed(1) + ' MB' : ''} · ${esc(v.audio || '')} · ${ago(v.created_at)}</span>
      <span class="sp"></span>${v.url ? `<a class="btn xs" href="${attr(v.url)}?download=true" download>${icon('download', 's')}Download</a><a class="btn xs" href="#composer" title="open the studio">${icon('movie', 's')}Studio</a>` : ''}</div>
      ${busy ? '<div class="progress" style="margin-top:8px"></div>' : ''}
      ${v.error ? `<div class="banner err" style="margin-top:8px">${icon('warn')}<div class="mono">${esc(v.error)}</div></div>` : ''}
      ${(v.warnings || []).length ? `<div class="muted body-s" style="margin-top:6px">${v.warnings.map(esc).join(' · ')}</div>` : ''}
      ${v.url ? `<video controls preload="none" poster="${attr(v.poster || '')}" src="${attr(v.url)}" style="margin-top:10px;max-height:460px;border-radius:var(--r);background:#000"></video>` : ''}</div>`;
  }).join('');
}

function bindVoiceVideo(el, s) {
  const voiceSel = el.querySelector('#voice');
  if (voiceSel && (!voices || Date.now() - (voices.at || 0) > 60000 || voices.provider !== (opts.voiceover && opts.voiceover.provider))) loadVoices();
  if (opts.composer && templates === null) api('/composer/templates').then((t) => { templates = t; if (!vform.template && t.length) vform.template = t[0].name || t[0]; if (tab === 'video') drawScript(); }).catch(() => { templates = []; });
  const tp = el.querySelector('#vtpl'); if (tp) tp.onchange = () => { vform.template = tp.value; };
  if (voiceSel) voiceSel.onchange = () => { vform.voice_id = voiceSel.value; };
  const speak = el.querySelector('#speak');
  if (speak) speak.onclick = async () => {
    const msg = el.querySelector('#vmsg');
    speak.disabled = true; msg.textContent = 'Speaking every scene… this takes a few seconds per line.';
    try {
      script = await post(`/scripts/${s.id}/voiceover`, { voice_id: (voiceSel && voiceSel.value) || null, fit_scenes: el.querySelector('#fitscenes').checked });
      toast('Voiceover ready — saved as v' + script.version); listSig = ''; drawScript();
    } catch (e) { voices = null; msg.textContent = e.message; toast(e.message, true); speak.disabled = false; loadVoices(); }
  };
  const mk = el.querySelector('#makevid');
  if (mk) mk.onclick = async () => {
    mk.disabled = true;
    try {
      const r = await post(`/scripts/${s.id}/video`, { preset: el.querySelector('#vpreset').value, audio: el.querySelector('#vaudio').value,
        template: (el.querySelector('#vtpl') || {}).value || null });
      toast(`Rendering ${r.scenes} scenes (${Math.round(r.seconds)}s)…`);
      if (r.warnings && r.warnings.length) toast(r.warnings[0], true);
      await pollVideos(s.id, true);
    } catch (e) { toast(e.message, true); mk.disabled = false; }
  };
  const pr = el.querySelector('#vpreset'); if (pr) pr.onchange = () => { vform.preset = pr.value; };
  const au = el.querySelector('#vaudio'); if (au) au.onchange = () => { vform.audio = au.value; drawScript(); };
  pollVideos(s.id, false);
}

async function loadVoices() {
  try {
    const v = await api('/tts/voices'); v.at = Date.now(); voices = v;
    const ids = new Set(v.voices.map((x) => x.id));
    const used = (script && script.extras && script.extras.voiceover || {}).voice_id;
    if (!ids.has(vform.voice_id)) vform.voice_id = (ids.has(used) && used) || v.default || (v.voices[0] || {}).id || '';
    if (tab === 'video') drawScript();
  } catch (e) { voices = { voices: [], usage: {}, at: Date.now() }; toast(e.message, true); }
}

async function pollVideos(sid, force) {
  clearTimeout(videoT);
  try { videos = await api(`/scripts/${sid}/video`); } catch { videos = []; }
  const box = root && root.querySelector('#vlist');
  if (box) box.innerHTML = videoList();
  else if (force) drawScript();
  if (videos.some((v) => v.status === 'QUEUED' || v.status === 'RENDERING')) videoT = setTimeout(() => pollVideos(sid, false), 2500);
  else if (force) drawScript();
}

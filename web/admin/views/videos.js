// Videos: library (analyse a video, filter by state) and the per-video detail with its knowledge blocks.
import { api, upload, esc, attr, icon, num, ago, dt, dur, bytes, tm, costOf, inr, toast, openDialog, emptyState } from '../core.js';

export const title = 'Videos';
export const subtitle = 'every analysed video and its knowledge blocks';
const STATES = ['RECEIVED', 'STABLE', 'QUEUED', 'UPLOADED', 'ANALYZING', 'BLOCKS_PARTIAL', 'BLOCKS_COMPLETE', 'EMBEDDING', 'INDEXED', 'CONTENT_CANDIDATE'];
const TERMINAL = ['BLOCKS_COMPLETE', 'INDEXED', 'CONTENT_CANDIDATE', 'FAILED'];
const hasResult = (j) => ['BLOCKS_COMPLETE', 'EMBEDDING', 'INDEXED', 'CONTENT_CANDIDATE'].includes(j.state);
const FILTERS = [['all', 'All', () => true], ['running', 'Running', (j) => !TERMINAL.includes(j.state)], ['CONTENT_CANDIDATE', 'Candidates', (j) => j.state === 'CONTENT_CANDIDATE'], ['INDEXED', 'Indexed', (j) => j.state === 'INDEXED'], ['BLOCKS_COMPLETE', 'Complete', (j) => j.state === 'BLOCKS_COMPLETE'], ['FAILED', 'Failed', (j) => j.state === 'FAILED']];
let root, ctx, jobs = [], filter = 'all', q = '', sel = null, result = null, tab = 'summary', listSig = '';
const jobsSig = (js) => JSON.stringify(js.map((j) => [j.id, j.state, j.block_counts, j.error]));

export async function render(el, params, c) { root = el; ctx = c; sel = params[0] || null; result = null; tab = 'summary'; if (c.query.f) filter = c.query.f; await draw(); }
export async function tick() { if (!root) return; if (sel) { const j = await api('/jobs/' + sel).catch(() => null); if (j && (!TERMINAL.includes(j.state) || (hasResult(j) && !result))) await drawDetail(j); } else { const js = await api('/jobs'); if (jobsSig(js) !== listSig) { jobs = js; drawList(); } } }
export function destroy() { root = null; }

async function draw() { if (sel) { const j = await api('/jobs/' + sel); await drawDetail(j); } else { jobs = await api('/jobs'); drawList(); } }

// ---------------------------------------------------------------- library
function drawList() {
  listSig = jobsSig(jobs);
  const counts = FILTERS.map(([k, l, f]) => [k, l, jobs.filter(f).length]);
  if (!counts.find((c) => c[0] === filter)[2]) filter = 'all';
  const needle = q.toLowerCase();
  const shown = jobs.filter(FILTERS.find((c) => c[0] === filter)[2]).filter((j) => !needle || j.source.name.toLowerCase().includes(needle) || j.id.includes(needle));
  root.innerHTML = `<div class="page-head"><div><h2>${jobs.length} videos</h2><p>Analysed by ${esc(ctx.overview.system.model)} · ${ctx.overview.jobs.minutes_of_video} minutes of footage</p></div><span class="sp"></span>
    <div class="row"><input type="search" id="vq" placeholder="filter by name or id" value="${attr(q)}" style="width:240px"><button class="btn filled" id="analyse">${icon('add')}Analyse a video</button></div></div>
  <div class="chips" style="margin-bottom:16px">${counts.filter(([k, , n]) => n || k === 'all').map(([k, l, n]) => `<span class="chip ${filter === k ? 'on' : ''}" data-f="${k}">${l}<b>${n}</b></span>`).join('')}</div>
  <div class="card"><div class="bd flush tw"><table><thead><tr><th>Video</th><th>State</th><th class="right">Length</th><th class="right">Blocks</th><th class="right">Quotes</th><th class="right">Cost</th><th class="right">Analysed</th><th></th></tr></thead><tbody>
  ${shown.map((j) => `<tr class="clickable" onclick="location.hash='videos/${esc(j.id)}'"><td><div class="ellipsis" style="font-weight:500;max-width:300px" title="${attr(j.source.name)}">${esc(j.source.name)}</div><div class="muted mono">${esc(j.source.kind)} · ${esc(j.id)} · ${esc(j.model)}</div></td><td><span class="tag ${esc(j.state)}">${esc(j.state)}</span>${j.error ? '<div class="muted body-s ellipsis" style="max-width:260px">' + esc(j.error).slice(0, 120) + '</div>' : ''}</td><td class="right mono">${dur(j.source.duration_seconds)}</td><td class="right mono">${Object.values(j.block_counts || {}).reduce((a, b) => a + b, 0) || '–'}</td><td class="right mono">${(j.block_counts || {}).quotes ?? '–'}</td><td class="right mono">${costOf(j.usage) ? inr(costOf(j.usage)) : '–'}</td><td class="right muted nowrap">${dt(j.created_at)}</td><td class="right nowrap">${j.source.path && hasResult(j) ? `<a class="btn xs tonal" href="#composer/${esc(j.id)}" onclick="event.stopPropagation()">${icon('movie', 's')}Reel</a>` : ''}</td></tr>`).join('') || `<tr><td colspan="8">${emptyState('video', 'No videos here', 'drop one into the watched folder or press Analyse')}</td></tr>`}
  </tbody></table></div></div>`;
  root.querySelectorAll('.chip[data-f]').forEach((s) => s.onclick = () => { filter = s.dataset.f; drawList(); });
  const vq = root.querySelector('#vq'); vq.oninput = () => { q = vq.value; const pos = vq.selectionStart; drawList(); const n = root.querySelector('#vq'); n.focus(); n.setSelectionRange(pos, pos); };
  root.querySelector('#analyse').onclick = analyseDialog;
}

export function analyseDialog() {
  let file = null;
  const d = openDialog(`<div class="dhd"><h3>Analyse a video</h3><p>Upload a file, paste a YouTube link, or point at a file under the allowed NAS roots.</p></div>
    <div class="dbd"><div class="drop" id="drop"><b>Drop a video here</b>or click to choose a file<input type="file" id="file" accept="video/*" hidden></div>
    <div class="field"><label>YouTube URL (public)</label><input type="url" id="url" placeholder="https://www.youtube.com/watch?v=…"></div>
    <div class="field"><label>NAS / local path</label><input type="text" id="path" placeholder="/mnt/nas/AI-Test/Convocation_2026.mp4"></div>
    <div class="msg" id="msg">YouTube sources are analysed without downloading — they can't be cut into reels.</div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="go">${icon('bolt')}Analyse</button></div>`, {
    onOpen(dlg) {
      const drop = dlg.querySelector('#drop'), fin = dlg.querySelector('#file'), msg = dlg.querySelector('#msg');
      const setFile = (f) => { file = f || null; drop.querySelector('b').textContent = f ? `${f.name} (${bytes(f.size)})` : 'Drop a video here'; };
      drop.onclick = () => fin.click(); fin.onchange = () => setFile(fin.files[0]);
      ['dragenter', 'dragover'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
      ['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }));
      drop.addEventListener('drop', (ev) => setFile(ev.dataTransfer.files[0]));
      dlg.querySelector('#go').onclick = async () => {
        const fd = new FormData(); const url = dlg.querySelector('#url').value.trim(), path = dlg.querySelector('#path').value.trim();
        if (file) fd.append('file', file); else if (url) fd.append('url', url); else if (path) fd.append('path', path); else { msg.textContent = 'Choose a file, or enter a URL or path.'; msg.className = 'msg err'; return; }
        const go = dlg.querySelector('#go'); go.disabled = true; msg.textContent = file ? 'Uploading…' : 'Submitting…'; msg.className = 'msg';
        try { const r = await upload('/analyze', fd); toast(r.deduplicated ? 'Already analysed — opening it' : 'Queued for analysis'); dlg.close(); location.hash = 'videos/' + r.job_id; }
        catch (e) { msg.textContent = e.message; msg.className = 'msg err'; go.disabled = false; }
      };
    },
  });
  return d;
}

// ---------------------------------------------------------------- detail
async function drawDetail(j) {
  if (hasResult(j) && !result) { try { result = await api('/jobs/' + j.id + '/result'); } catch { result = null; } }
  const s = j.source, failed = j.state === 'FAILED', last = j.stages[j.stages.length - 1];
  const steps = STATES.map((st) => { const hit = j.stages.some((x) => x.state === st); const cls = failed ? (hit ? 'done' : '') : (st === j.state && !TERMINAL.includes(j.state) ? 'cur' : hit ? 'done' : ''); return `<span class="step ${cls}">${st}</span>`; }).join('') + (failed ? '<span class="step fail">FAILED</span>' : '');
  const usd = costOf(j.usage);
  let h = `<div class="page-head"><div><a href="#videos" class="body-s">${icon('chevron', 's')} All videos</a><h2 class="ellipsis" style="max-width:900px" title="${attr(s.name)}">${esc(s.name)}</h2><p><span class="tag ${esc(j.state)}">${esc(j.state)}</span> &nbsp;${esc(s.kind)} · ${dur(s.duration_seconds)} · ${bytes(s.size_bytes)} · ${esc(j.provider)} / ${esc(j.model)} · ${dt(j.created_at)}</p></div><span class="sp"></span>
    <div class="row">${s.path && hasResult(j) ? `<a class="btn tonal" href="#composer/${esc(j.id)}">${icon('movie')}Make a reel</a>` : ''}${hasResult(j) ? `<a class="btn outlined" href="#drafts?new=1&job=${esc(j.id)}">${icon('article')}Write an article</a>` : ''}${s.url ? `<a class="btn" href="${attr(s.url)}" target="_blank">${icon('open')}Source</a>` : ''}</div></div>`;
  h += `<div class="card" style="margin-bottom:20px"><div class="bd" style="padding:18px 20px"><div class="stepper">${steps}</div><div class="muted body-s" style="margin-top:8px">last: ${esc(last.state)} at ${tm(last.at)} ${Object.keys(last.detail || {}).length ? '· <code class="mono">' + esc(JSON.stringify(last.detail)) + '</code>' : ''}</div>
    ${j.error ? `<div class="banner err" style="margin-top:12px">${icon('warn')}<div class="mono">${esc(j.error)}</div></div>` : ''}${j.warnings && j.warnings.length ? `<div class="banner warn" style="margin-top:12px">${icon('info')}<div>${j.warnings.map(esc).join(' · ')}</div></div>` : ''}
    <dl class="kv" style="padding:16px 0 0;grid-template-columns:120px 1fr"><dt>Source</dt><dd class="mono">${esc(s.path || s.url || s.name)}</dd><dt>Media id</dt><dd class="mono">${esc(j.media_id || '–')}</dd><dt>Job id</dt><dd class="mono">${esc(j.id)}</dd>${s.sha256 ? `<dt>SHA-256</dt><dd class="mono ellipsis">${esc(s.sha256)}</dd>` : ''}</dl>
    ${result ? `<div class="stats" style="margin-top:14px"><span>processing <b>${result.processing_seconds}s</b></span><span>tokens in <b>${num((result.usage || {}).input_tokens)}</b></span><span>out <b>${num((result.usage || {}).output_tokens)}</b></span><span>thought <b>${num((result.usage || {}).thought_tokens)}</b></span><span>cost <b>${inr(usd)}</b></span><span>repaired <b>${result.repaired}</b></span><span>prompt <b>${esc(result.prompt_version)}</b></span></div>` : ''}
  </div></div>`;
  if (result) h += `<div class="card"><div class="bd flush">${renderBlocks(result)}</div></div>`;
  else if (!failed) h += `<div class="card"><div class="bd"><div class="loading"><span class="spin" style="margin-right:10px"></span> Analysis in progress — this page updates on its own.</div></div></div>`;
  root.innerHTML = h;
  root.querySelectorAll('.tab').forEach((t) => t.onclick = () => { tab = t.dataset.t; drawDetail(j); });
}

function renderBlocks(r) {
  const a = r.analysis;
  const tabs = [['summary', 'Summary', 1], ['events', 'Events', a.events.length], ['people', 'People', a.people.length], ['topics', 'Topics', a.topics.length], ['key_moments', 'Key moments', a.key_moments.length], ['quotes', 'Quotes', a.quotes.length], ['transcript', 'Transcript', a.transcript.length], ['media', 'Media', a.media.length], ['content_opportunities', 'Opportunities', a.content_opportunities.length], ['json', 'Raw JSON', 0]];
  let h = '<div class="tabs" style="padding:0 12px">' + tabs.map(([k, l, n]) => `<span class="tab ${tab === k ? 'on' : ''}" data-t="${k}">${l}${n ? `<b>${n}</b>` : ''}</span>`).join('') + '</div><div style="padding:20px">';
  const conf = (c) => c == null ? '' : `<span class="conf">conf ${(c * 100).toFixed(0)}%</span>`; const tsl = (t) => (t || []).map((x) => `<span class="ts">${esc(x)}</span>`).join('');
  const none = '<div class="empty">none</div>';
  switch (tab) {
    case 'summary': h += `<div class="block"><h4>${esc(a.video.title)}</h4><p class="m">${esc(a.video.video_type)} · ${esc(a.video.language)} · observed ${esc(a.video.observed_duration || '–')}</p><p>${esc(a.video.description)}</p></div><div class="block"><h4>Summary</h4><p><b>${esc(a.summary.short_summary)}</b></p><p>${esc(a.summary.detailed_summary).replace(/\n/g, '<br>')}</p><ul>${a.summary.key_points.map((k) => '<li>' + esc(k) + '</li>').join('')}</ul></div>`; break;
    case 'events': h += a.events.map((e) => `<div class="block">${conf(e.confidence)}<h4>${esc(e.name)}</h4><p class="m">${esc(e.date || 'date unknown')} · ${esc(e.venue || 'venue unknown')} · ${esc(e.organizer || '')}</p><p>${esc(e.description)}</p></div>`).join('') || none; break;
    case 'people': h += a.people.map((p) => `<div class="block">${conf(p.confidence)}<h4>${esc(p.name)} ${p.identified_by ? '<span class="tag">' + esc(p.identified_by) + '</span>' : ''}</h4><p class="m">${esc(p.role || 'role unknown')}</p><p>${esc(p.context)}</p><p>${tsl(p.timestamps)}</p></div>`).join('') || none; break;
    case 'topics': h += a.topics.map((t) => `<div class="block">${conf(t.confidence)}<h4>${esc(t.name)}</h4><p>${esc(t.evidence)}</p><p>${tsl(t.timestamps)}</p></div>`).join('') || none; break;
    case 'key_moments': h += a.key_moments.map((k) => `<div class="block"><span class="ts">${esc(k.timestamp)}</span><span class="tag">${esc(k.importance)}</span><p>${esc(k.description)}</p></div>`).join('') || none; break;
    case 'quotes': h += a.quotes.map((q) => `<div class="block"><span class="ts">${esc(q.timestamp)}</span><b>${esc(q.speaker)}</b><p>“${esc(q.text)}”</p><p class="m">${esc(q.source_reference)}</p></div>`).join('') || none; break;
    case 'transcript': h += a.transcript.map((t) => `<div class="block"><span class="ts">${esc(t.start_time)}–${esc(t.end_time)}</span><b>${esc(t.speaker)}</b> <span class="m">${esc(t.language || '')}</span><p>${esc(t.text)}</p></div>`).join('') || none; break;
    case 'media': h += a.media.map((m) => `<div class="block"><span class="ts">${esc(m.timestamp)}</span>${(m.suitable_for || []).map((x) => '<span class="tag">' + esc(x) + '</span>').join(' ')}<p>${esc(m.description)}</p></div>`).join('') || none; break;
    case 'content_opportunities': h += a.content_opportunities.map((c) => `<div class="block">${conf(c.confidence)}<h4>${esc(c.title)}</h4><p>${esc(c.reason)}</p><p class="m">for ${esc(c.audience)} · ${(c.suggested_formats || []).map((x) => '<span class="tag">' + esc(x) + '</span>').join(' ')}</p></div>`).join('') || none; break;
    case 'json': h += `<pre class="json">${esc(JSON.stringify(r, null, 2))}</pre>`; break;
  }
  return h + '</div>';
}

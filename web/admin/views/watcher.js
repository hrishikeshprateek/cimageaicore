import { api, post, put, esc, attr, icon, num, ago, tm, toast, confirmDialog, openDialog, emptyState } from '../core.js';
import { evRow } from './overview.js';

export const title = 'Folder watcher';
export const subtitle = 'videos dropped on the NAS are picked up automatically';
let root, ctx, lastSig = '';
const sigOf = (w, idx, nas) => JSON.stringify([w.enabled, w.running, w.paused, w.roots, w.files_seen, w.indexed, w.pending_stable, w.queued_total, w.deduplicated_total, w.embedded_total, w.errors_total, w.active_jobs, w.recent, idx.map((i) => [i.path, i.action, i.job_id]), nas && nas.roots.map((r) => [r.path, r.exists, r.videos, r.source])]);

export async function render(el, params, c) { root = el; ctx = c; lastSig = ''; await draw(); }
export async function tick() { if (root) await draw(); }

const timing = (w) => `last scan ${w.last_scan_at ? ago(w.last_scan_at) + ' (' + w.last_scan_seconds + 's)' : 'never'} · next ${w.next_scan_at ? tm(w.next_scan_at) : '–'} · ${w.scans} scans since start · ${w.active_jobs} analyses running`;

async function draw() {
  const [w, idx, nas] = await Promise.all([api('/watcher'), api('/watcher/index'), api('/nas/roots').catch(() => null)]);
  const now = sigOf(w, idx, nas);
  if (now === lastSig && root.querySelector('#w-timing')) { root.querySelector('#w-timing').textContent = timing(w); return; }
  lastSig = now;
  const on = w.running && !w.paused;
  root.innerHTML = `<div class="kpis">
    <div class="kpi ${on ? 'hot' : ''}"><div class="l">Status</div><div class="v" style="font-size:24px">${w.enabled ? (w.paused ? 'Paused' : (w.running ? 'Running' : 'Stopped')) : 'Disabled'}</div><div class="s">every ${w.interval_seconds}s · stable after ${w.stable_seconds}s · max ${w.max_active_jobs} at once</div></div>
    <div class="kpi"><div class="l">Files seen</div><div class="v">${num(w.files_seen)}</div><div class="s"><b>${w.indexed}</b> indexed · <b>${w.pending_stable}</b> waiting</div></div>
    <div class="kpi"><div class="l">Queued</div><div class="v">${num(w.queued_total)}</div><div class="s">${w.deduplicated_total} duplicates skipped</div></div>
    <div class="kpi"><div class="l">Blocks embedded</div><div class="v">${num(w.embedded_total)}</div><div class="s">by the sweeper between scans</div></div>
    <div class="kpi"><div class="l">Errors</div><div class="v" style="color:${w.errors_total ? 'var(--error)' : 'inherit'}">${num(w.errors_total)}</div><div class="s">retried on the next cycle</div></div>
  </div>
  <div class="card" style="margin-bottom:20px"><div class="bd" style="padding:18px 20px;display:flex;gap:14px;flex-wrap:wrap;align-items:center">
    <div class="grow" style="min-width:260px"><div class="overline">Watched folders</div>
     <div style="margin-top:6px;display:flex;flex-direction:column;gap:6px">${(nas ? nas.roots : w.roots.map((r) => ({ path: r, exists: true, source: 'env' }))).map((r) => `<div class="row" style="gap:8px"><span class="mono" style="word-break:break-all">${esc(r.path)}</span>${r.exists === false ? '<span class="tag err">not found — mount / connect the NAS</span>' : r.readable === false ? '<span class="tag err">not readable</span>' : `<span class="tag ok">${r.videos ?? 0} video${r.videos === 1 ? '' : 's'}${r.folders ? ' · ' + r.folders + ' folders' : ''}</span>`}${r.source === 'env' ? '<span class="tag" title="set in .env (WATCH_ROOTS / NAS_WATCH_DIR)">.env</span>' : `<button class="btn xs danger" data-rmroot="${attr(r.path)}" title="stop watching this folder">${icon('close', 's')}</button>`}</div>`).join('') || '<span class="muted">none yet — pick a folder</span>'}</div>
     <div class="muted body-s" style="margin-top:8px">${nas ? esc(nas.hint) : ''}</div>
     <div class="muted body-s" style="margin-top:6px" id="w-timing">${timing(w)}</div></div>
    <button class="btn filled" id="wPick">${icon('folder')}Pick a folder…</button>
    ${w.enabled ? `<button class="btn tonal" id="wScan">${icon('refresh')}Scan now</button>${w.paused ? `<button class="btn ok" id="wResume">${icon('play')}Resume</button>` : `<button class="btn outlined" id="wPause">${icon('pause')}Pause</button>`}` : `<span class="muted body-s">Enable with <code>WATCHER_ENABLED=true</code> and <code>WATCH_ROOTS=/path</code>, then restart the API.</span>`}
  </div></div>
  <div class="g2"><div class="card"><div class="hd"><h3>Indexed files</h3><span class="sp"></span><span class="muted body-s">${idx.length}</span></div><div class="bd flush tw"><table><thead><tr><th>File</th><th>Result</th><th>Job</th><th>When</th><th></th></tr></thead><tbody>
    ${idx.slice(0, 200).map((i) => `<tr><td><div class="ellipsis" style="font-weight:500;max-width:320px" title="${attr(i.path)}">${esc(i.path.split('/').pop())}</div><div class="muted mono">${(i.size / 1e6).toFixed(1)} MB</div></td><td><span class="tag ${esc(i.action)}">${esc(i.action)}</span>${i.detail ? '<div class="muted body-s">' + esc(i.detail) + '</div>' : ''}</td><td>${i.job_id ? `<a class="mono" href="#videos/${esc(i.job_id)}">${esc(i.job_id)}</a>` : '–'}</td><td class="muted nowrap">${ago(i.at)}</td><td class="right"><button class="btn xs" data-forget="${attr(i.path)}" title="forget: the next scan submits it again">re-scan</button></td></tr>`).join('') || `<tr><td colspan="5">${emptyState('folder', 'Nothing indexed yet', 'drop a video into the watched folder')}</td></tr>`}
  </tbody></table></div></div>
  <div class="card"><div class="hd"><h3>Recent events</h3></div><div class="bd">${(w.recent || []).map(evRow).join('') || '<div class="empty">no events yet</div>'}</div></div></div>`;
  const pk = root.querySelector('#wPick'); if (pk) pk.onclick = () => pickFolderDialog(nas);
  root.querySelectorAll('[data-rmroot]').forEach((x) => x.onclick = async () => { const keep = nas.roots.filter((r) => r.source === 'ui' && r.path !== x.dataset.rmroot).map((r) => r.path); if (!(await confirmDialog({ title: 'Stop watching this folder?', body: x.dataset.rmroot + ' — videos already analysed stay in the library.', ok: 'Stop watching', danger: true }))) return; try { await put('/nas/roots', { roots: keep }); toast('Folder removed'); } catch (e) { toast(e.message, true); } lastSig = ''; draw(); });
  const b = root.querySelector('#wScan'); if (b) b.onclick = async () => { b.disabled = true; try { const r = await post('/watcher/scan?sync=true'); toast(`Scanned: ${r.seen} seen, ${r.queued} queued, ${r.deduplicated} duplicates, ${r.embedded} embedded`); } catch (e) { toast(e.message, true); } draw(); };
  const p = root.querySelector('#wPause'); if (p) p.onclick = async () => { await post('/watcher/pause'); toast('Watcher paused'); draw(); };
  const r = root.querySelector('#wResume'); if (r) r.onclick = async () => { await post('/watcher/resume'); toast('Watcher resumed'); draw(); };
  root.querySelectorAll('[data-forget]').forEach((x) => x.onclick = async () => { try { await post('/watcher/forget', { path: x.dataset.forget }); toast('Will be picked up on the next scan'); draw(); } catch (e) { toast(e.message, true); } });
}


// ---------------------------------------------------------------- folder picker
function pickFolderDialog(nas) {
  let cur = null, data = null;
  const dlg = openDialog(`<div class="dhd"><h3>Pick the folder to watch</h3><p>Every video dropped into the chosen folder (and its sub-folders) is analysed automatically. Browse the NAS or any mounted location.</p></div>
    <div class="dbd"><div class="row" style="gap:6px"><button class="btn xs" id="pkUp" title="up one level">${icon('chevron', 's')}</button><span class="mono grow" id="pkPath" style="word-break:break-all">…</span></div>
      <div class="list" id="pkList" style="max-height:52vh;overflow:auto;border:1px solid var(--line);border-radius:8px"><div class="loading"><span class="spin"></span></div></div>
      <div class="muted body-s" id="pkInfo"></div><div class="msg" id="pkMsg"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="pkUse" disabled>${icon('check')}Watch this folder</button></div>`, {
    onOpen(d) { load(null); d.querySelector('#pkUp').onclick = () => load(data && data.parent ? data.parent : null); d.querySelector('#pkUse').onclick = use; },
  });
  async function load(path) {
    const list = dlg.querySelector('#pkList'); list.innerHTML = '<div class="loading"><span class="spin"></span></div>';
    try { data = await api('/nas/browse' + (path ? '?path=' + encodeURIComponent(path) : '')); } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    cur = data.path; dlg.querySelector('#pkPath').textContent = cur || 'Locations'; dlg.querySelector('#pkUse').disabled = !cur;
    dlg.querySelector('#pkUp').disabled = !data.parent && !cur;
    const already = new Set(((nas && nas.roots) || []).map((r) => r.path));
    list.innerHTML = data.folders.map((f) => `<div class="li clickable" data-p="${attr(f.path)}"><div class="avatar">${icon('folder')}</div><div class="grow"><div class="t">${esc(data.places ? f.path : f.name)}</div><div class="d">${f.videos ? f.videos + ' video' + (f.videos === 1 ? '' : 's') + ' inside' : 'no videos directly inside'}${already.has(f.path) ? ' · already watched' : ''}</div></div>${icon('chevron')}</div>`).join('')
      + (data.videos && data.videos.length ? `<div class="li"><div class="avatar">${icon('video')}</div><div class="grow"><div class="t">${data.videos.length} video file${data.videos.length === 1 ? '' : 's'} here</div><div class="d">${data.videos.slice(0, 4).map((v) => esc(v.name)).join(' · ')}${data.videos.length > 4 ? ' · …' : ''}</div></div></div>` : '')
      || `<div class="empty">${data.places ? 'No mounted locations found. Mount the NAS on this machine (see docs/DEPLOY.md) or set NAS_WATCH_DIR.' : 'empty folder'}</div>`;
    list.querySelectorAll('.li.clickable').forEach((n) => n.onclick = () => load(n.dataset.p));
    dlg.querySelector('#pkInfo').textContent = cur ? (data.videos.length ? `Watching “${cur}” will pick up ${data.videos.length} video(s) here plus anything in its sub-folders.` : `Watching “${cur}” picks up videos dropped here or in its sub-folders.`) : '';
  }
  async function use() {
    if (!cur) return;
    const keep = ((nas && nas.roots) || []).filter((r) => r.source === 'ui').map((r) => r.path);
    if (!keep.includes(cur)) keep.push(cur);
    const btn = dlg.querySelector('#pkUse'); btn.disabled = true;
    try { await put('/nas/roots', { roots: keep }); dlg.close(); toast('Now watching ' + cur); lastSig = ''; draw(); }
    catch (e) { const m = dlg.querySelector('#pkMsg'); m.textContent = e.message; m.className = 'msg err'; btn.disabled = false; }
  }
}

import { api, post, esc, attr, icon, num, ago, tm, toast, emptyState } from '../core.js';
import { evRow } from './overview.js';

export const title = 'Folder watcher';
export const subtitle = 'videos dropped on the NAS are picked up automatically';
let root, ctx, lastSig = '';
const sigOf = (w, idx) => JSON.stringify([w.enabled, w.running, w.paused, w.roots, w.files_seen, w.indexed, w.pending_stable, w.queued_total, w.deduplicated_total, w.embedded_total, w.errors_total, w.active_jobs, w.recent, idx.map((i) => [i.path, i.action, i.job_id])]);

export async function render(el, params, c) { root = el; ctx = c; lastSig = ''; await draw(); }
export async function tick() { if (root) await draw(); }

const timing = (w) => `last scan ${w.last_scan_at ? ago(w.last_scan_at) + ' (' + w.last_scan_seconds + 's)' : 'never'} · next ${w.next_scan_at ? tm(w.next_scan_at) : '–'} · ${w.scans} scans since start · ${w.active_jobs} analyses running`;

async function draw() {
  const [w, idx] = await Promise.all([api('/watcher'), api('/watcher/index')]);
  const now = sigOf(w, idx);
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
    <div class="grow" style="min-width:260px"><div class="overline">Watched folders</div><div class="mono" style="margin-top:4px">${w.roots.map(esc).join('<br>') || '<span class="muted">none — set WATCH_ROOTS</span>'}</div>
     <div class="muted body-s" style="margin-top:6px" id="w-timing">${timing(w)}</div></div>
    ${w.enabled ? `<button class="btn tonal" id="wScan">${icon('refresh')}Scan now</button>${w.paused ? `<button class="btn ok" id="wResume">${icon('play')}Resume</button>` : `<button class="btn outlined" id="wPause">${icon('pause')}Pause</button>`}` : `<span class="muted body-s">Enable with <code>WATCHER_ENABLED=true</code> and <code>WATCH_ROOTS=/path</code>, then restart the API.</span>`}
  </div></div>
  <div class="g2"><div class="card"><div class="hd"><h3>Indexed files</h3><span class="sp"></span><span class="muted body-s">${idx.length}</span></div><div class="bd flush tw"><table><thead><tr><th>File</th><th>Result</th><th>Job</th><th>When</th><th></th></tr></thead><tbody>
    ${idx.slice(0, 200).map((i) => `<tr><td><div class="ellipsis" style="font-weight:500;max-width:320px" title="${attr(i.path)}">${esc(i.path.split('/').pop())}</div><div class="muted mono">${(i.size / 1e6).toFixed(1)} MB</div></td><td><span class="tag ${esc(i.action)}">${esc(i.action)}</span>${i.detail ? '<div class="muted body-s">' + esc(i.detail) + '</div>' : ''}</td><td>${i.job_id ? `<a class="mono" href="#videos/${esc(i.job_id)}">${esc(i.job_id)}</a>` : '–'}</td><td class="muted nowrap">${ago(i.at)}</td><td class="right"><button class="btn xs" data-forget="${attr(i.path)}" title="forget: the next scan submits it again">re-scan</button></td></tr>`).join('') || `<tr><td colspan="5">${emptyState('folder', 'Nothing indexed yet', 'drop a video into the watched folder')}</td></tr>`}
  </tbody></table></div></div>
  <div class="card"><div class="hd"><h3>Recent events</h3></div><div class="bd">${(w.recent || []).map(evRow).join('') || '<div class="empty">no events yet</div>'}</div></div></div>`;
  const b = root.querySelector('#wScan'); if (b) b.onclick = async () => { b.disabled = true; try { const r = await post('/watcher/scan?sync=true'); toast(`Scanned: ${r.seen} seen, ${r.queued} queued, ${r.deduplicated} duplicates, ${r.embedded} embedded`); } catch (e) { toast(e.message, true); } draw(); };
  const p = root.querySelector('#wPause'); if (p) p.onclick = async () => { await post('/watcher/pause'); toast('Watcher paused'); draw(); };
  const r = root.querySelector('#wResume'); if (r) r.onclick = async () => { await post('/watcher/resume'); toast('Watcher resumed'); draw(); };
  root.querySelectorAll('[data-forget]').forEach((x) => x.onclick = async () => { try { await post('/watcher/forget', { path: x.dataset.forget }); toast('Will be picked up on the next scan'); draw(); } catch (e) { toast(e.message, true); } });
}

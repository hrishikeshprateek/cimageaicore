// Overview: what the pipeline did, what needs a decision.
import { api, esc, icon, num, ago, tm, dur, toast, post, emptyState } from '../core.js';

export const title = 'Overview';
export const subtitle = 'what the pipeline did, and what needs you';
let root = null, ctx = null, lastSig = '', attHtml = '', attSig = '';

export async function render(el, params, c) { root = el; ctx = c; lastSig = ''; draw(); }
export async function tick(c) { ctx = c; draw(); }
const sig = (ov) => JSON.stringify({ j: ov.jobs, b: ov.blocks, c: ov.content, cost: ov.cost, a: (ov.audit || []).slice(0, 10).map((x) => x.id), w: ov.watcher && [ov.watcher.paused, ov.watcher.running, ov.watcher.files_seen, ov.watcher.indexed, ov.watcher.pending_stable, ov.watcher.queued_total, ov.watcher.deduplicated_total, ov.watcher.embedded_total, ov.watcher.errors_total, (ov.watcher.recent || []).slice(0, 5)] });

export function evRow(e) {
  return `<div class="ev"><span class="t">${tm(e.at)}</span><div><span class="tag ${esc(e.action)}">${esc(e.action)}</span> ${e.path ? '<span class="mono">' + esc(e.path.split('/').pop()) + '</span>' : ''}${e.job_id ? ` <a class="mono" href="#videos/${esc(e.job_id)}">${esc(e.job_id)}</a>` : ''}${e.detail ? '<div class="d">' + esc(e.detail) + '</div>' : ''}</div></div>`;
}
export function auditRow(a) {
  const d = a.detail || {}; const raw = Object.keys(d).length ? JSON.stringify(d) : ''; const det = raw ? esc(raw.length > 110 ? raw.slice(0, 110) + '…' : raw) : '';
  const link = a.entity_type === 'job' ? `#videos/${esc(a.entity_id)}` : a.entity_type === 'draft' ? `#drafts/${esc(a.entity_id)}` : null;
  return `<div class="ev"><span class="t" title="${esc(a.at)}">${tm(a.at)}</span><div class="grow"><div><span class="a">${esc(a.action)}</span> <span class="muted">by ${esc(a.actor)}</span>${a.entity_id ? ' · ' + (link ? `<a class="mono" href="${link}">` : '<span class="mono">') + esc(a.entity_type || '') + ' ' + esc(a.entity_id) + (link ? '</a>' : '</span>') : ''}</div>${det ? '<div class="d mono ellipsis" title="' + esc(raw) + '">' + det + '</div>' : ''}</div></div>`;
}

function draw() {
  const ov = ctx.overview; if (!ov || !root) return;
  const now = sig(ov);
  if (now === lastSig) { patchWatcher(ov.watcher); return; }   // nothing changed: leave the DOM alone (no flicker)
  lastSig = now;
  const j = ov.jobs, b = ov.blocks, e = b.embeddings || {}, c = ov.content || {}, w = ov.watcher, cost = ov.cost;
  const embPct = e.total ? Math.round(100 * e.embedded / e.total) : 0, rev = c.awaiting_review || 0, d = c.drafts || {}, o = c.opportunities || {};
  const st = j.by_state || {}; const failed = st.FAILED || 0;
  const kpis = `<div class="kpis">
    <div class="kpi hot"><div class="l">Awaiting your review</div><div class="v">${rev}</div><div class="s">${d.approved || 0} approved · ${d.rejected || 0} rejected</div></div>
    <div class="kpi"><div class="l">Videos analysed</div><div class="v">${num(j.total)}</div><div class="s"><b>${j.active}</b> in progress · ${j.minutes_of_video} min of footage</div></div>
    <div class="kpi"><div class="l">Knowledge blocks</div><div class="v">${num(b.total)}</div><div class="s"><b>${embPct}%</b> embedded${e.stale ? ' · ' + e.stale + ' stale' : ''}${e.pending ? ' · ' + e.pending + ' pending' : ''}</div><div class="bar ok" style="margin-top:10px"><i style="width:${embPct}%"></i></div></div>
    <div class="kpi"><div class="l">Opportunities</div><div class="v">${num(o.new || 0)}</div><div class="s">new · ${o.accepted || 0} accepted · ${o.drafted || 0} drafted</div></div>
    <div class="kpi gold"><div class="l">AI spend (estimate)</div><div class="v">₹${cost.inr.toFixed(cost.inr < 10 ? 2 : 0)}</div><div class="s">${num(cost.input_tokens)} in · ${num(cost.output_tokens)} out tokens</div></div>
  </div>`;
  const opTotal = Object.values(o).reduce((a, b2) => a + b2, 0), drTotal = Object.values(d).reduce((a, b2) => a + b2, 0), done = (st.BLOCKS_COMPLETE || 0) + (st.INDEXED || 0) + (st.CONTENT_CANDIDATE || 0);
  const moving = !!(j.active || d.generating || (w && w.pending_stable) || e.pending);
  const node = (cls, href, ic, cnt, lbl, sub, pill) => `<a class="node ${cls}" href="${href}" title="${esc(lbl)} — open">${pill ? `<span class="pill">${esc(pill)}</span>` : ''}<span class="go">Open ›</span><div class="ring">${icon(ic)}</div><div class="cnt">${cnt}</div><div class="lbl">${lbl}</div><div class="sub">${sub}</div></a>`;
  const pipe = `<div class="card"><div class="hd"><h3>Pipeline</h3><span class="sp"></span><span class="body-s muted">${moving ? '<span class="dot" style="animation:pulse 1.4s infinite"></span>work is moving' : 'quiet'} · nothing leaves without approval</span></div><div class="bd" style="padding-top:0"><div class="flow ${moving ? 'active' : ''}">
    ${node(w && w.running && !w.paused ? (w.pending_stable ? 'live' : 'done') : 'idle', '#watcher', 'folder', w ? num(w.files_seen) : '–', 'Watch folder', w ? (w.running ? (w.paused ? 'paused' : `${w.queued_total} queued · ${w.deduplicated_total} dupes`) : 'off') : 'off', w && w.errors_total ? w.errors_total + ' err' : '')}
    ${node(j.active ? 'live' : (failed ? 'err' : 'idle'), failed ? '#videos?f=FAILED' : '#videos?f=running', 'movie', j.active, 'Analyse', j.active ? 'running now' : (failed ? `${failed} failed · click to see` : 'idle'), '')}
    ${node(done ? 'done' : 'idle', '#videos', 'grid_view', num(b.total), 'Blocks', `${done} videos done`, '')}
    ${node(e.pending || e.stale ? 'live' : (e.total ? 'done' : 'idle'), '#search', 'hub', `${embPct}<small>%</small>`, 'Embed', e.pending || e.stale ? `${(e.pending || 0) + (e.stale || 0)} to do` : 'up to date · local', '')}
    ${node(o.new ? 'att' : (opTotal ? 'done' : 'idle'), '#opps', 'auto_awesome', o.new || 0, 'Opportunities', o.new ? 'new to triage' : `${opTotal} total`, '')}
    ${node(d.generating ? 'live' : (drTotal ? 'done' : 'idle'), '#drafts?f=generating', 'edit_note', d.generating || 0, 'Draft', d.generating ? 'writing…' : `${drTotal} written · ${ov.system.auto_draft ? 'auto' : 'manual'}`, '')}
    ${node(rev ? 'att' : (d.approved ? 'done' : 'idle'), '#drafts?f=new', 'rate_review', rev, 'Review', rev ? 'waiting for you' : `${d.approved || 0} approved`, '')}
    ${node('next off', '#drafts?f=approved', 'send', d.approved || 0, 'Publish', 'next phase', '')}
  </div>
  <div class="funnel">
    <div class="fseg"><div class="v">${done}</div><div class="k">videos analysed</div><div class="bar ok"><i style="width:${j.total ? Math.round(100 * done / j.total) : 0}%"></i></div><div class="pct">${j.total ? Math.round(100 * done / j.total) : 0}% of ${j.total} submitted</div></div>
    <div class="fseg"><div class="v">${opTotal}</div><div class="k">opportunities found</div><div class="bar"><i style="width:${done ? Math.min(100, Math.round(100 * opTotal / Math.max(done, 1))) : 0}%"></i></div><div class="pct">${done ? (opTotal / done).toFixed(1) : '0'} per video</div></div>
    <div class="fseg"><div class="v">${drTotal}</div><div class="k">drafts written</div><div class="bar"><i style="width:${opTotal ? Math.min(100, Math.round(100 * drTotal / opTotal)) : 0}%"></i></div><div class="pct">${opTotal ? Math.round(100 * drTotal / opTotal) : 0}% of opportunities</div></div>
    <div class="fseg"><div class="v">${d.approved || 0}</div><div class="k">approved</div><div class="bar ok"><i style="width:${drTotal ? Math.round(100 * (d.approved || 0) / drTotal) : 0}%"></i></div><div class="pct">${drTotal ? Math.round(100 * (d.approved || 0) / drTotal) : 0}% of drafts</div></div>
    <div class="fseg"><div class="v">₹${cost.inr.toFixed(0)}</div><div class="k">AI spend so far</div><div class="bar gold"><i style="width:100%"></i></div><div class="pct">≈ ₹${done ? (cost.inr / done).toFixed(1) : '0'} per video</div></div>
  </div>
  </div></div>`;
  const attention = `<div class="card"><div class="hd"><h3>Needs your attention</h3><span class="sp"></span><a class="btn sm tonal" href="#drafts">Open review ${icon('chevron', 's')}</a></div><div class="bd flush" id="att">${attHtml || '<div class="loading"><span class="spin"></span></div>'}</div></div>`;
  const watcher = `<div class="card"><div class="hd"><h3>Folder watcher</h3><span class="sp"></span>${w ? `<span class="tag ${w.paused ? 'warn' : (w.running ? 'ok' : 'err')}">${w.paused ? 'paused' : (w.running ? 'running' : 'off')}</span>` : ''}<a class="btn sm" href="#watcher">Details ${icon('chevron', 's')}</a></div><div class="bd">
    ${w ? `<div class="mono muted" style="margin-bottom:10px">${w.roots.map(esc).join('<br>')}</div><div class="kv" style="padding:0;grid-template-columns:110px 1fr;font-size:13px"><dt>last scan</dt><dd id="w-last">${w.last_scan_at ? ago(w.last_scan_at) + ' · ' + w.last_scan_seconds + 's' : 'not yet'}</dd><dt>next scan</dt><dd id="w-next">${w.next_scan_at ? tm(w.next_scan_at) : '–'}</dd><dt>files</dt><dd>${w.files_seen} seen · ${w.indexed} indexed · ${w.pending_stable} waiting</dd><dt>totals</dt><dd>${w.queued_total} queued · ${w.deduplicated_total} duplicates · ${w.embedded_total} blocks embedded${w.errors_total ? ' · <span style="color:var(--error)">' + w.errors_total + ' errors</span>' : ''}</dd></div>
    <div style="margin-top:12px">${(w.recent || []).slice(0, 5).map(evRow).join('') || '<div class="muted body-s">no events yet</div>'}</div>` : emptyState('folder', 'Watcher is not running', 'set WATCHER_ENABLED=true')}
  </div></div>`;
  const recent = `<div class="card"><div class="hd"><h3>Recent videos</h3><span class="sp"></span><a class="btn sm" href="#videos">Library ${icon('chevron', 's')}</a></div><div class="bd flush tw"><table><thead><tr><th>Video</th><th>State</th><th class="right">Length</th><th class="right">Blocks</th><th class="right">When</th></tr></thead><tbody>
    ${(j.recent || []).map((r) => `<tr class="clickable" onclick="location.hash='videos/${esc(r.id)}'"><td><div class="ellipsis" style="font-weight:500;max-width:340px" title="${esc(r.name)}">${esc(r.name)}</div><div class="muted mono">${esc(r.kind)} · ${esc(r.id)}</div></td><td><span class="tag ${esc(r.state)}">${esc(r.state)}</span></td><td class="right mono">${dur(r.duration_seconds)}</td><td class="right mono">${r.blocks}</td><td class="right muted nowrap">${ago(r.created_at)}</td></tr>`).join('') || `<tr><td colspan="5">${emptyState('video', 'No videos yet')}</td></tr>`}
  </tbody></table></div></div>`;
  const activity = `<div class="card"><div class="hd"><h3>Activity</h3><span class="sp"></span><a class="btn sm" href="#activity">All ${icon('chevron', 's')}</a></div><div class="bd">${(ov.audit || []).slice(0, 10).map(auditRow).join('') || '<div class="empty">nothing logged yet</div>'}</div></div>`;
  root.innerHTML = kpis + pipe + `<div class="g2" style="margin-top:20px"><div class="stack">${attention}${recent}</div><div class="stack">${watcher}${activity}</div></div>`;
  if (attHtml) bindAttention(root.querySelector('#att'));
  drawAttention();
}
function patchWatcher(w) {
  if (!w || !root) return; const l = root.querySelector('#w-last'), n = root.querySelector('#w-next');
  if (l) l.textContent = w.last_scan_at ? ago(w.last_scan_at) + ' · ' + w.last_scan_seconds + 's' : 'not yet';
  if (n) n.textContent = w.next_scan_at ? tm(w.next_scan_at) : '–';
}

async function drawAttention() {
  const el = root.querySelector('#att'); if (!el) return;
  if (!(ctx.overview.content && ctx.overview.content.available)) { el.innerHTML = '<div class="empty">Content features need PostgreSQL.</div>'; return; }
  try {
    const ds = (await api('/drafts')).filter((d) => d.status === 'new' || d.status === 'in_review').slice(0, 6);
    const os = (await api('/opportunities?status=new')).slice(0, 4);
    const html = '<div class="list">' + (ds.length ? ds.map((d) => `<div class="li clickable" onclick="location.hash='drafts/${esc(d.id)}'"><div class="avatar">${icon('article')}</div><div class="grow"><div class="t ellipsis">${esc(d.title || d.brief)}</div><div class="d"><span class="tag ${esc(d.status)}">${esc(d.status).replace('_', ' ')}</span> v${d.version} · ${esc(d.model || '')} · ${ago(d.created_at)}${d.warnings && d.warnings.length ? ' · ⚠ ' + d.warnings.length : ''}</div></div><div class="trail"><button class="btn sm ok" data-approve="${esc(d.id)}">${icon('check', 's')}Approve</button></div></div>`).join('') : '<div class="li"><div class="muted">No drafts waiting — all caught up ✓</div></div>') +
      (os.length ? `<div class="li" style="min-height:0;padding:14px 20px 4px"><span class="overline">New opportunities</span></div>` + os.map((o) => `<div class="li"><div class="avatar">${icon('spark')}</div><div class="grow"><div class="t ellipsis">${esc(o.title)}</div><div class="d">${esc(o.source_name || '')} · ${Math.round((o.confidence || 0) * 100)}% confidence</div></div><div class="trail"><button class="btn sm filled" data-draft="${esc(o.id)}">Draft</button></div></div>`).join('') : '') + '</div>';
    const s2 = JSON.stringify([ds.map((d) => [d.id, d.status, d.version]), os.map((o) => o.id)]);
    if (s2 === attSig && attHtml && !el.querySelector('.loading')) return;   // unchanged: keep the DOM
    attSig = s2; attHtml = html; el.innerHTML = html; bindAttention(el);
  } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function bindAttention(el) {
  if (!el) return;
    el.querySelectorAll('[data-approve]').forEach((b) => b.onclick = async (ev) => { ev.stopPropagation(); try { await post('/drafts/' + b.dataset.approve + '/status', { status: 'approved' }); toast('Approved'); ctx.refresh(); } catch (e) { toast(e.message, true); } });
    el.querySelectorAll('[data-draft]').forEach((b) => b.onclick = async () => { b.disabled = true; try { await post('/opportunities/' + b.dataset.draft + '/status', { status: 'accepted' }); const d = await post('/drafts', { opportunity_id: b.dataset.draft }); toast('Drafting… it will appear under Review'); location.hash = 'drafts/' + d.id; } catch (e) { toast(e.message, true); b.disabled = false; } });
}

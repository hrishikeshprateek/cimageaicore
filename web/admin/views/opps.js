// Content opportunities: angles the analysis found; accept to draft.
import { api, post, esc, icon, ago, toast, emptyState } from '../core.js';

export const title = 'Opportunities';
export const subtitle = 'angles the analysis found; accept to draft an article';
let root, ctx, filter = 'new', lastSig = '';

export async function render(el, params, c) { root = el; ctx = c; lastSig = ''; if (c.query.f !== undefined) filter = c.query.f; await draw(); }
export async function tick() { if (root) await draw(true); }

async function draw(fromTick = false) {
  if (!(ctx.overview.content && ctx.overview.content.available)) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('spark', 'Opportunities need PostgreSQL', 'set DATABASE_URL')}</div></div>`; return; }
  const all = await api('/opportunities');
  const now = JSON.stringify([filter, all.map((o) => [o.id, o.status])]); if (fromTick && now === lastSig) return; lastSig = now;
  const counts = {}; all.forEach((o) => counts[o.status] = (counts[o.status] || 0) + 1);
  const list = all.filter((o) => !filter || o.status === filter);
  root.innerHTML = `<div class="page-head"><div><h2>${counts.new || 0} new opportunities</h2><p>${ctx.overview.system.auto_draft ? 'Auto-draft is on: the best new opportunity of every video is drafted automatically.' : 'Auto-draft is off: accept an opportunity to have it written.'}</p></div><span class="sp"></span>
    <div class="chips">${[['new', 'New'], ['accepted', 'Accepted'], ['drafted', 'Drafted'], ['dismissed', 'Dismissed'], ['', 'All']].map(([k, l]) => `<span class="chip ${filter === k ? 'on' : ''}" data-f="${k}">${l}<b>${k ? counts[k] || 0 : all.length}</b></span>`).join('')}</div></div>
  <div class="gauto">${list.map((o) => `<div class="card op"><div class="src">${o.job_id ? `<a class="ellipsis" style="max-width:220px" href="#videos/${esc(o.job_id)}">${esc(o.source_name || 'video')}</a>` : esc(o.source_name || 'manual')} · ${ago(o.created_at)} · <span class="tag ${esc(o.status)}">${esc(o.status)}</span></div><h4>${esc(o.title)}</h4><p>${esc(o.reason || '')}</p>${o.audience ? `<p class="body-s">for <b>${esc(o.audience)}</b></p>` : ''}<div class="chips" style="gap:6px">${(o.suggested_formats || []).map((f) => '<span class="tag">' + esc(f) + '</span>').join('')}</div><div class="conf"><span>confidence</span><div class="bar ${(o.confidence || 0) >= .7 ? 'ok' : ''}"><i style="width:${Math.round((o.confidence || 0) * 100)}%"></i></div><span class="mono">${Math.round((o.confidence || 0) * 100)}%</span></div>
    <div class="foot">${o.status === 'new' ? `<button class="btn filled sm" data-a="draft" data-id="${esc(o.id)}">${icon('article', 's')}Accept &amp; draft</button><button class="btn sm" data-a="dismiss" data-id="${esc(o.id)}">Dismiss</button>` : o.status === 'accepted' ? `<button class="btn filled sm" data-a="draft" data-id="${esc(o.id)}">Draft article</button>` : o.status === 'dismissed' ? `<button class="btn sm" data-a="restore" data-id="${esc(o.id)}">Restore</button>` : `<a class="btn sm tonal" href="#drafts">See drafts ${icon('chevron', 's')}</a>`}</div></div>`).join('') || `<div class="card" style="grid-column:1/-1"><div class="bd">${emptyState('spark', 'Nothing here')}</div></div>`}</div>`;
  root.querySelectorAll('.chip[data-f]').forEach((s) => s.onclick = () => { filter = s.dataset.f; draw(); });
  root.querySelectorAll('[data-a]').forEach((b) => b.onclick = async () => {
    const id = b.dataset.id, a = b.dataset.a; b.disabled = true;
    try {
      if (a === 'dismiss') await post('/opportunities/' + id + '/status', { status: 'dismissed' });
      else if (a === 'restore') await post('/opportunities/' + id + '/status', { status: 'new' });
      else { await post('/opportunities/' + id + '/status', { status: 'accepted' }); const d = await post('/drafts', { opportunity_id: id }); toast('Drafting… opening it'); location.hash = 'drafts/' + d.id; return; }
      toast('Done'); draw();
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });
}

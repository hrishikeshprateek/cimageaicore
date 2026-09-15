import { api, esc, emptyState } from '../core.js';
import { auditRow } from './overview.js';

export const title = 'Activity log';
export const subtitle = 'audit trail of everything the system and people did';
let root, actor = '', lastSig = '';

export async function render(el, params, ctx) { root = el; lastSig = ''; await draw(ctx); }
export async function tick(ctx) { if (root) await draw(ctx, true); }

async function draw(ctx, fromTick = false) {
  const rows = await api('/audit?limit=200' + (actor ? '&actor=' + encodeURIComponent(actor) : ''));
  const now = JSON.stringify([actor, rows.length, rows[0] && rows[0].id]); if (fromTick && now === lastSig) return; lastSig = now;
  const actors = [...new Set(rows.map((a) => a.actor))];
  root.innerHTML = `<div class="card"><div class="hd"><h3>${rows.length} events</h3><span class="sp"></span><div class="chips" id="afl"><span class="chip ${actor === '' ? 'on' : ''}" data-f="">all</span>${actors.map((a) => `<span class="chip ${actor === a ? 'on' : ''}" data-f="${esc(a)}">${esc(a)}</span>`).join('')}</div></div><div class="bd">${rows.map(auditRow).join('') || emptyState('history', ctx.overview.system.store === 'postgres' ? 'Nothing logged yet' : 'The audit log needs PostgreSQL')}</div></div>`;
  root.querySelectorAll('#afl .chip').forEach((s) => s.onclick = () => { actor = s.dataset.f; draw(ctx); });
}

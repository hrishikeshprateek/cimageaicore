// Shell + hash router. Views live in ./views/*.js and export {title, subtitle, render(root, params, ctx), tick?(ctx), destroy?()}.
import { api, esc, icon, ago, toast } from './core.js';
import * as overview from './views/overview.js';
import * as videos from './views/videos.js';
import * as search from './views/search.js';
import * as opps from './views/opps.js';
import * as drafts from './views/drafts.js';
import * as composer from './views/composer.js';
import * as watcher from './views/watcher.js';
import * as activity from './views/activity.js';
import * as publishing from './views/publishing.js';

const VIEWS = { overview, videos, search, opps, drafts, composer, publishing, watcher, activity };
const NAV = [
  ['Monitor', [['overview', 'Overview', 'dashboard'], ['videos', 'Videos', 'video'], ['search', 'Search', 'search']]],
  ['Content', [['opps', 'Opportunities', 'spark'], ['drafts', 'Review drafts', 'review'], ['composer', 'Reels studio', 'movie'], ['publishing', 'Publishing', 'send']]],
  ['System', [['watcher', 'Folder watcher', 'folder'], ['activity', 'Activity log', 'history']]],
  ['Tools', [['qrstudio', 'QR studio', 'qr_code', 'https://qrstudio.cimage.in/']]],   // external: opens in a new tab
];

export const ctx = { overview: null, view: null, params: [], go, refresh: () => route(true) };
let current = null, currentName = null, pollT = null, ovT = null;

export function go(hash) { location.hash = hash; }

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs] = raw.split('?');
  const parts = path.split('/').filter(Boolean);
  const name = VIEWS[parts[0]] ? parts[0] : 'overview';
  const params = parts.slice(1).map(decodeURIComponent);
  const query = Object.fromEntries(new URLSearchParams(qs || ''));
  return { name, params, query };
}

async function loadOverview() {
  try { ctx.overview = await api('/admin/overview'); renderChrome(); } catch (e) { setFoot(`<span class="dot off"></span>API unreachable — ${esc(e.message)}`); }
}
const setFoot = (h) => { const f = document.getElementById('navfoot'); if (f) f.innerHTML = h; };

function renderChrome() {
  const ov = ctx.overview; if (!ov) return;
  const s = ov.system, c = ov.content || {}, w = ov.watcher;
  setFoot(`<div><span class="dot ${s.store === 'postgres' ? '' : 'warn'}"></span><b>${esc(s.store)}</b> store</div><div>AI <b>${esc(s.provider)}</b> · ${esc(s.model)}</div><div>embed <b>${esc(s.embedder)}</b> · ${esc(s.embedding_model)}</div><div>v${esc(s.version)} · auto-draft ${s.auto_draft ? 'on' : 'off'}</div>`);
  const badge = (id, n, warn = false) => { const b = document.getElementById(id); if (!b) return; b.hidden = !n; b.textContent = n || ''; b.classList.toggle('warn', warn); };
  badge('b-drafts', c.awaiting_review || 0); badge('b-opps', (c.opportunities || {}).new || 0); badge('b-watcher', w && w.errors_total ? w.errors_total : 0, true); badge('b-videos', ov.jobs.active || 0);
  document.getElementById('pills').innerHTML =
    `<span class="status-pill"><span class="dot ${ov.jobs.active ? '' : 'off'}" style="${ov.jobs.active ? 'animation:pulse 1.4s infinite' : 'background:var(--outline-variant)'}"></span>${ov.jobs.active ? `<b>${ov.jobs.active}</b> analysing` : 'idle'}</span>` +
    `<span class="status-pill">${icon('folder', 's')}${w && w.running ? (w.paused ? 'watcher paused' : `watcher <b>${w.last_scan_at ? ago(w.last_scan_at) : 'starting'}</b>`) : 'watcher off'}</span>` +
    `<span class="status-pill">${icon('wallet', 's')}<b>₹${ov.cost.inr.toFixed(0)}</b> spent</span>`;
}

function renderNav() {
  const { name } = parseHash();
  document.getElementById('navitems').innerHTML = NAV.map(([sec, items]) => `<div class="sec">${sec}</div>` + items.map(([id, label, ic, ext]) => ext
    ? `<a class="item ext" href="${ext}" target="_blank" rel="noopener" title="${label} — opens in a new tab">${icon(ic)}<span class="lbl">${label}</span><span class="ext-ic">${icon('open', 's')}</span></a>`
    : `<a class="item ${id === name ? 'on' : ''}" href="#${id}" data-v="${id}">${icon(ic)}<span class="lbl">${label}</span><span class="badge" id="b-${id}" hidden></span></a>`).join('')).join('');
}

async function route(force = false) {
  const { name, params, query } = parseHash();
  const view = VIEWS[name];
  const same = name === currentName && !force && JSON.stringify(params) === JSON.stringify(ctx.params) && JSON.stringify(query) === JSON.stringify(ctx.query || {});
  if (same) return;
  if (current && current.destroy && (name !== currentName || force)) { try { current.destroy(); } catch { /* ignore */ } }
  currentName = name; current = view; ctx.view = name; ctx.params = params; ctx.query = query;
  document.querySelectorAll('#navitems .item').forEach((a) => a.classList.toggle('on', a.dataset.v === name));
  document.getElementById('title').textContent = view.title;
  document.getElementById('subtitle').textContent = view.subtitle || '';
  document.title = `${view.title} · CIMAGE AI`;
  const root = document.getElementById('view');
  root.classList.remove('enter'); void root.offsetWidth; root.classList.add('enter');
  root.innerHTML = '<div class="progress" style="margin:16px 0"></div>';
  if (!ctx.overview) await loadOverview();
  try { await view.render(root, params, ctx); } catch (e) { root.innerHTML = `<div class="banner err">${icon('warn')}<div><b>Could not load this page.</b><br>${esc(e.message)}</div></div>`; }
  document.getElementById('view').scrollTop = 0;
}

function boot() {
  renderNav();
  window.addEventListener('hashchange', () => route(false));
  document.getElementById('refresh').onclick = () => { loadOverview(); route(true); };
  const sb = document.getElementById('q');
  sb.addEventListener('keydown', (e) => { if (e.key === 'Enter' && sb.value.trim()) { go('search?q=' + encodeURIComponent(sb.value.trim())); sb.blur(); } });
  document.addEventListener('keydown', (e) => { if (e.key === '/' && !e.target.matches('input,textarea,select,[contenteditable]')) { e.preventDefault(); sb.focus(); } });
  loadOverview().then(() => route(true));
  ovT = setInterval(() => { if (!document.hidden) loadOverview(); }, 6000);
  pollT = setInterval(() => { if (!document.hidden && current && current.tick) { try { current.tick(ctx); } catch { /* ignore */ } } }, 5000);
}
boot();

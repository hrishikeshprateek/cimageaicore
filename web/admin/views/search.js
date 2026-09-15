// Search the knowledge base: hybrid (keyword + local embeddings), semantic, or keyword.
import { api, esc, attr, icon, emptyState } from '../core.js';

export const title = 'Search';
export const subtitle = 'ask the knowledge base in plain words — Hindi or English';
let root, q = '', mode = 'hybrid', type = '';
const TYPES = ['', 'quote', 'transcript', 'key_moment', 'person', 'topic', 'event', 'media', 'content_opportunity'];

export async function render(el, params, ctx) { root = el; q = ctx.query.q || q; mode = ctx.query.mode || mode; draw(); if (q) run(); }

function draw() {
  root.innerHTML = `<div class="card" style="margin-bottom:20px"><div class="bd" style="padding:20px">
    <div class="row"><input type="search" id="sq" value="${attr(q)}" placeholder="who spoke about placements? · प्लेसमेंट के बारे में किसने बात की?" style="flex:1;height:52px;font-size:16px;border-radius:var(--r-full);padding:0 22px" autofocus><button class="btn filled" id="go" style="height:52px;padding:0 26px">${icon('search')}Search</button></div>
    <div class="row" style="margin-top:14px;gap:16px"><div class="seg" id="mode">${[['hybrid', 'Hybrid'], ['vector', 'Semantic'], ['keyword', 'Keyword']].map(([k, l]) => `<label><input type="radio" name="mode" value="${k}" ${mode === k ? 'checked' : ''}>${l}</label>`).join('')}</div>
    <div class="chips" id="types">${TYPES.map((t) => `<span class="chip ${type === t ? 'on' : ''}" data-t="${t}">${t ? t.replace('_', ' ') : 'all types'}</span>`).join('')}</div></div>
    <div class="muted body-s" style="margin-top:10px">Hybrid fuses full-text and vector ranks; semantic search runs on the local EmbeddingGemma model, so it works without internet and finds English blocks from Hindi queries.</div>
  </div></div><div id="hits"></div>`;
  const sq = root.querySelector('#sq');
  const trigger = () => { q = sq.value.trim(); if (!q) return; history.replaceState(null, '', '#search?q=' + encodeURIComponent(q) + '&mode=' + mode); run(); };
  root.querySelector('#go').onclick = trigger; sq.addEventListener('keydown', (e) => { if (e.key === 'Enter') trigger(); });
  root.querySelectorAll('#mode input').forEach((r) => r.onchange = () => { mode = r.value; if (q) trigger(); });
  root.querySelectorAll('#types .chip').forEach((c) => c.onclick = () => { type = c.dataset.t; root.querySelectorAll('#types .chip').forEach((x) => x.classList.toggle('on', x === c)); if (q) run(); });
}

async function run() {
  const el = root.querySelector('#hits'); el.innerHTML = '<div class="progress"></div>';
  try {
    const hits = await api('/search?q=' + encodeURIComponent(q) + '&mode=' + mode + '&limit=30' + (type ? '&block_type=' + type : ''));
    el.innerHTML = hits.length ? `<div class="overline" style="margin:4px 0 12px">${hits.length} results</div>` + hits.map((h) => `<div class="hit" onclick="location.hash='videos/${esc(h.job_id)}'"><span class="ty">${esc(h.block_type)}</span> ${h.timestamp ? `<span class="tag mono">${esc(h.timestamp)}</span>` : ''} <span class="tag" style="float:right">${(h.matched_by || []).join(' + ') || mode}</span><div style="margin-top:6px">${esc(h.text)}</div><div class="src">${esc(h.source_name)} · ${esc(h.block_id)}</div></div>`).join('')
      : emptyState('search', 'No blocks matched', 'try different words or switch to semantic');
  } catch (e) { el.innerHTML = `<div class="banner err">${icon('warn')}<div>${esc(e.message)}</div></div>`; }
}

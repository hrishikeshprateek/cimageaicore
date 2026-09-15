// Drafts: the review queue. Read, edit, manage pictures, approve / reject. Also "New article" from a brief.
import { api, post, put, upload, esc, attr, icon, ago, dt, words, md, toast, confirmDialog, openDialog, emptyState } from '../core.js';
import { targetDialog } from './publishing.js';

export const title = 'Review drafts';
export const subtitle = 'read, edit, approve — nothing is published without you';
let root, ctx, drafts = [], sel = null, draft = null, tab = 'article', filter = '', hl = null, listSig = '';
const draftsSig = (ds) => JSON.stringify([filter, sel, ds.map((d) => [d.id, d.status, d.version, d.title])]);
let CANDS = [], LIB = [], PLACE = [];

export async function render(el, params, c) {
  root = el; ctx = c; sel = params[0] || sel; draft = null; tab = 'article';
  if (c.query.f !== undefined) filter = c.query.f;
  if (c.query.new) { newArticleDialog(c.query.job || ''); }
  await draw(true);
}
export async function tick() { if (!root || tab === 'edit' || tab === 'images') return; const ds = await api('/drafts'); if (draftsSig(ds) === listSig) return; await draw(false); }
export function destroy() { root = null; }

async function draw(force) {
  if (!(ctx.overview.content && ctx.overview.content.available)) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('article', 'Drafts need PostgreSQL', 'set DATABASE_URL')}</div></div>`; return; }
  drafts = await api('/drafts'); listSig = draftsSig(drafts);
  if (sel && (force || !draft || drafts.find((d) => d.id === sel)?.status !== draft.status)) { try { draft = await api('/drafts/' + sel); } catch { draft = null; sel = null; } }
  const counts = { '': drafts.length }; drafts.forEach((d) => counts[d.status] = (counts[d.status] || 0) + 1);
  const fl = [['', 'All'], ['new', 'New'], ['in_review', 'In review'], ['approved', 'Approved'], ['rejected', 'Rejected'], ['generating', 'Writing'], ['failed', 'Failed']].filter(([k]) => k === '' || counts[k]).map(([k, l]) => `<span class="chip ${filter === k ? 'on' : ''}" data-f="${k}">${l}<b>${counts[k] || 0}</b></span>`).join('');
  const list = drafts.filter((d) => !filter || d.status === filter);
  const rev = (ctx.overview.content || {}).awaiting_review || 0;
  root.innerHTML = `<div class="page-head"><div><h2>${rev ? `${rev} waiting for a decision` : 'Drafts'}</h2><p>Approve, request changes, or reject — every decision is logged.</p></div><span class="sp"></span><button class="btn filled" id="newart">${icon('add')}New article</button></div>
  <div class="review"><div class="card"><div class="hd" style="padding-bottom:8px"><div class="chips">${fl}</div></div><div class="dlist list" id="dl">${list.map((d) => `<div class="li clickable ${d.id === sel ? 'sel' : ''}" data-id="${esc(d.id)}"><div class="avatar">${icon('article')}${d.hero_image_id ? `<img src="/api/v1/images/${esc(d.hero_image_id)}" alt="" style="position:absolute;inset:0" onerror="this.remove()">` : ''}</div><div class="grow"><div class="t" style="white-space:normal;line-height:19px">${esc(d.title || d.brief)}</div><div class="d"><span class="tag ${esc(d.status)}">${esc(d.status).replace('_', ' ')}</span> v${d.version} · ${ago(d.created_at)}${d.warnings && d.warnings.length ? ' · ⚠ ' + d.warnings.length : ''}</div></div></div>`).join('') || emptyState('article', 'No drafts here')}</div></div>
  <div class="card reader" id="reader">${sel ? '<div class="loading"><span class="spin"></span></div>' : emptyState('review', 'Select a draft to read it', 'or press New article to write one from a brief')}</div></div>`;
  root.querySelector('#newart').onclick = () => newArticleDialog('');
  root.querySelectorAll('.chip[data-f]').forEach((s) => s.onclick = () => { filter = s.dataset.f; draw(false); });
  root.querySelectorAll('#dl .li').forEach((n) => n.onclick = async () => { sel = n.dataset.id; tab = 'article'; draft = null; CANDS = []; LIB = []; history.replaceState(null, '', '#drafts/' + sel); root.querySelectorAll('#dl .li').forEach((x) => x.classList.toggle('sel', x === n)); root.querySelector('#reader').innerHTML = '<div class="loading"><span class="spin"></span></div>'; try { draft = await api('/drafts/' + sel); } catch (e) { toast(e.message, true); } drawReader(); });
  if (sel) drawReader();
}

function figure(id, cls) { const im = (draft && draft.images || []).find((x) => x.image_id === id); if (!im) return ''; return `<figure class="${cls || ''}"><img src="/api/v1/images/${esc(id)}" alt="${attr(im.alt_text || '')}" loading="lazy">${im.caption ? '<figcaption>' + esc(im.caption) + '</figcaption>' : ''}</figure>`; }

function drawReader() {
  const el = root.querySelector('#reader'); const d = draft; if (!el) return;
  if (!d) { el.innerHTML = emptyState('review', 'Select a draft to read it'); return; }
  const hero = (d.images || []).find((i) => i.placement === 'hero'); const seo = d.seo || {}, so = d.social || {}; const canDecide = ['new', 'in_review'].includes(d.status);
  const ev = (d.evidence && d.evidence.blocks) || [];
  const tabs = [['article', 'Article'], ['images', `Pictures (${(d.images || []).length})`], ['seo', 'SEO & social'], ['evidence', `Evidence (${(d.citations || []).length} cited)`], ['edit', 'Edit'], ['versions', `Versions (${d.version})`], ['published', 'Published']];
  let h = (hero ? `<img class="hero" src="/api/v1/images/${esc(hero.image_id)}" alt="${attr(hero.alt_text || '')}">` : '') + `<div class="rhead"><h2>${esc(d.title || d.brief)}</h2><div class="meta"><span class="tag ${esc(d.status)}">${esc(d.status).replace('_', ' ')}</span><span>v${d.version}</span><span>·</span><span>${words(d.body_markdown)} words</span><span>·</span><span>${esc(d.model || '')} / ${esc(d.prompt_version || '')}</span><span>·</span><span>${esc(d.depth || 'standard')}</span><span>·</span><span>${dt(d.created_at)}</span>${d.opportunity_id ? `<span>·</span><span class="mono">opp ${esc(d.opportunity_id)}</span>` : ''}</div>
    ${d.warnings && d.warnings.length ? `<div class="banner warn" style="margin-top:12px">${icon('info')}<div>${d.warnings.map(esc).join(' · ')}</div></div>` : ''}${d.error ? `<div class="banner err" style="margin-top:12px">${icon('warn')}<div class="mono">${esc(d.error)}</div></div>` : ''}</div>`;
  h += `<div class="actions">${canDecide ? `<button class="btn ok" data-s="approved">${icon('check')}Approve</button>${d.status !== 'in_review' ? '<button class="btn outlined" data-s="in_review">Needs changes</button>' : ''}<button class="btn danger" data-s="rejected">Reject</button>` : d.status === 'approved' ? `<button class="btn filled" id="publish">${icon('send')}Publish</button><button class="btn outlined" data-s="in_review">Reopen</button>` : d.status === 'rejected' ? '<button class="btn outlined" data-s="in_review">Reopen</button>' : ''}${d.status !== 'generating' ? `<button class="btn" id="regen">${icon('refresh')}Regenerate</button><button class="btn" id="regen2" title="rewrite as an in-depth feature">In-depth</button>` : ''}<span class="sp"></span><span class="muted body-s">${d.status === 'generating' ? '<span class="spin"></span> the writer is working…' : ''}</span></div>`;
  h += '<div class="tabs" style="padding:0 16px">' + tabs.map(([k, l]) => `<span class="tab ${tab === k ? 'on' : ''}" data-t="${k}">${l}</span>`).join('') + '</div>';
  if (tab === 'article') h += `<div class="article" id="article">${hero ? '' : ''}${md(d.body_markdown || '', { figure }) || '<p class="muted">no text yet</p>'}</div><p class="muted body-s" style="padding:0 28px 24px">Superscripts are evidence ids — click one to see the block. They are stripped on publish.</p>`;
  if (tab === 'images') h += renderImagesTab(d);
  if (tab === 'seo') h += `<dl class="kv"><dt>Slug</dt><dd class="mono">${esc(d.slug || '–')}</dd><dt>SEO title</dt><dd>${esc(seo.seo_title || '–')}</dd><dt>Meta description</dt><dd>${esc(seo.meta_description || '–')} <span class="muted body-s">(${(seo.meta_description || '').length} chars)</span></dd><dt>Excerpt</dt><dd>${esc(seo.excerpt || '–')}</dd><dt>Tags</dt><dd>${(seo.tags || []).map((t) => '<span class="tag">' + esc(t) + '</span>').join(' ') || '–'}</dd><dt>Evidence gaps</dt><dd>${(seo.evidence_gaps || []).map(esc).join('<br>') || 'none reported'}</dd></dl><div style="padding:0 28px 24px">${Object.entries(so).map(([k, v]) => `<div class="overline" style="margin-top:12px">${esc(k)}</div><div class="snippet">${esc(typeof v === 'string' ? v : JSON.stringify(v, null, 1))}</div>`).join('') || '<span class="muted">no social snippets</span>'}</div>`;
  if (tab === 'evidence') { const cited = new Set((d.citations || []).map((c) => c.block_id)); h += `<div style="padding:16px 28px"><p class="muted body-s" style="margin-top:0">${ev.length} blocks were offered to the writer from ${(d.evidence.sources || []).map((s) => esc(s.source_name) + ' (' + s.blocks + ')').join(', ') || 'the library'}. Highlighted = cited in the article.</p>${ev.map((b) => `<div class="evb ${cited.has(b.block_id) ? 'cited' : ''} ${hl === b.block_id ? 'hl' : ''}" id="ev-${esc(b.block_id)}"><span class="ty">${esc(b.block_type)}</span> ${b.timestamp ? '<span class="tag mono">' + esc(b.timestamp) + '</span> ' : ''}${esc(b.text)}<div class="src">${esc(b.source_name)} · ${esc(b.block_id)}</div></div>`).join('') || '<div class="empty">no evidence stored</div>'}</div>`; }
  if (tab === 'edit') h += `<div class="editpane"><input type="text" id="eTitle" value="${attr(d.title || '')}" placeholder="Title"><textarea id="eBody">${esc(d.body_markdown || '')}</textarea><div class="row" style="margin-top:12px"><button class="btn filled" id="eSave">Save as version ${d.version + 1}</button><span class="muted body-s">Saving moves the draft to “in review”. Keep [id=…] citation markers and [img=…] picture markers where they are.</span></div></div>`;
  if (tab === 'versions') h += `<div style="padding:16px 28px" id="vers"><div class="loading"><span class="spin"></span></div></div>`;
  if (tab === 'published') h += `<div style="padding:16px 28px" id="pubs"><div class="loading"><span class="spin"></span></div></div>`;
  el.innerHTML = h;
  const pb = el.querySelector('#publish'); if (pb) pb.onclick = () => publishDialog(d);
  if (tab === 'published') drawPublished(el, d);
  el.querySelectorAll('.tab').forEach((t) => t.onclick = async () => { tab = t.dataset.t; if (tab === 'images' && !CANDS.length && !LIB.length) await loadImagesFor(d); drawReader(); });
  el.querySelectorAll('.actions [data-s]').forEach((b) => b.onclick = async () => { const s = b.dataset.s;
    if (s === 'rejected' && !(await confirmDialog({ title: 'Reject this draft?', body: 'It stays in the list as rejected and can be reopened.', ok: 'Reject', danger: true }))) return;
    if (s === 'approved') { let auto = []; try { auto = (await api('/publish/targets')).filter((t) => t.enabled && t.auto_on_approval); } catch { /* publishing not configured */ }
      const body = auto.length ? `It will be sent to ${auto.map((t) => `${t.name} (${t.mode === 'publish' ? 'live' : 'as a WP draft'})`).join(', ')} right away.` : 'No website publishes automatically yet — you can publish from the draft afterwards.';
      if (!(await confirmDialog({ title: 'Approve this article?', body, ok: auto.length ? 'Approve & publish' : 'Approve' }))) return; }
    try { draft = await post('/drafts/' + d.id + '/status', { status: s }); toast(s === 'approved' ? 'Approved' : s === 'rejected' ? 'Rejected' : 'Marked as needing changes'); if (s === 'approved') tab = 'published'; ctx.refresh(); } catch (e) { toast(e.message, true); } });
  const rg = el.querySelector('#regen'); if (rg) rg.onclick = async () => { if (!(await confirmDialog({ title: 'Regenerate this article?', body: 'The writer starts over with the same brief; the current text stays as a version.', ok: 'Regenerate' }))) return; try { await post('/drafts/' + d.id + '/regenerate'); toast('Regenerating…'); draft = null; draw(true); } catch (e) { toast(e.message, true); } };
  const rg2 = el.querySelector('#regen2'); if (rg2) rg2.onclick = async () => { if (!(await confirmDialog({ title: 'Rewrite as an in-depth feature?', body: '~1500 words with takeaways, FAQ and pictures. The current text stays as a version.', ok: 'Rewrite' }))) return; try { await post('/drafts/' + d.id + '/regenerate?depth=in_depth'); toast('Rewriting in depth…'); draft = null; draw(true); } catch (e) { toast(e.message, true); } };
  const sv = el.querySelector('#eSave'); if (sv) sv.onclick = async () => { sv.disabled = true; try { draft = await put('/drafts/' + d.id, { title: el.querySelector('#eTitle').value, body_markdown: el.querySelector('#eBody').value }); tab = 'article'; toast('Saved as v' + draft.version); draw(false); } catch (e) { toast(e.message, true); sv.disabled = false; } };
  el.querySelectorAll('sup.cite').forEach((s) => s.onclick = () => { hl = s.dataset.id; tab = 'evidence'; drawReader(); const e = document.getElementById('ev-' + hl); if (e) e.scrollIntoView({ block: 'center' }); });
  if (tab === 'images') bindImagesTab(el, d);
  if (tab === 'versions') api('/drafts/' + d.id + '/versions').then((vs) => { const v = el.querySelector('#vers'); if (v) v.innerHTML = vs.map((x) => `<div class="evb"><b>v${x.version}</b> · ${esc(x.edited_by || x.author || 'writer')} · ${dt(x.created_at)}<div class="muted body-s" style="margin-top:4px">${esc(x.title || '')}</div></div>`).join('') || '<div class="empty">no versions</div>'; }).catch((e) => { const v = el.querySelector('#vers'); if (v) v.innerHTML = `<div class="empty">${esc(e.message)}</div>`; });
}

// ---------------------------------------------------------------- new article from a brief
async function newArticleDialog(jobId) {
  let jobs = []; try { jobs = (await api('/jobs')).filter((j) => j.block_counts && Object.keys(j.block_counts).length); } catch { /* ignore */ }
  openDialog(`<div class="dhd"><h3>New article</h3><p>The writer only uses evidence from the knowledge base and pictures from your own videos and photo library.</p></div>
    <div class="dbd"><div class="field"><label>Brief</label><textarea id="brief" placeholder="e.g. Article about our robotics lab for prospective parents" style="min-height:80px"></textarea></div>
    <div class="field"><label>Anchor video</label><select id="job"><option value="">none — search the whole library</option>${jobs.map((j) => `<option value="${esc(j.id)}" ${j.id === jobId ? 'selected' : ''}>${esc(j.source.name.slice(0, 70))} · ${j.source.path ? 'local' : 'online'}</option>`).join('')}</select></div>
    <div class="row gap16"><div class="field grow"><label>Depth</label><select id="depth"><option value="standard">standard article (~1000 words)</option><option value="in_depth">in-depth feature (~1500 words, takeaways + FAQ)</option></select></div><label class="check" style="margin-top:22px"><input type="checkbox" id="pics" checked> pictures</label></div>
    <div class="msg" id="m"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="go">${icon('article')}Draft it</button></div>`, {
    onOpen(dlg) {
      dlg.querySelector('#go').onclick = async () => {
        const b = dlg.querySelector('#brief').value.trim(); const m = dlg.querySelector('#m'); if (b.length < 5) { m.textContent = 'Write a short brief first.'; m.className = 'msg err'; return; }
        const go = dlg.querySelector('#go'); go.disabled = true; m.textContent = 'Starting the writer…'; m.className = 'msg';
        try { const d = await post('/drafts', { brief: b, job_id: dlg.querySelector('#job').value || null, depth: dlg.querySelector('#depth').value, include_images: dlg.querySelector('#pics').checked }); dlg.close(); toast('Drafting — it appears here when ready'); location.hash = 'drafts/' + d.id; sel = d.id; draft = null; draw(true); }
        catch (e) { m.textContent = e.message; m.className = 'msg err'; go.disabled = false; }
      };
    },
  });
}

// ---------------------------------------------------------------- pictures
function renderImagesTab(d) {
  PLACE = (d.images || []).map((x) => ({ ...x })); const jobId = d.evidence && d.evidence.job_id; const offered = new Set((d.evidence && d.evidence.offered_images) || []);
  const card = (c, kind) => { const used = PLACE.find((p) => p.image_id === c.id); return `<div class="pic ${used ? (used.placement === 'hero' ? 'hero' : 'used') : ''}"><img src="${attr(c.url)}" loading="lazy" title="${attr(c.description || '')}"><div class="b"><div class="ellipsis">${esc(c.description || '(no description)')}</div><div class="k">${kind === 'frame' ? (c.kind === 'frame' ? (c.timestamp ? 'frame ' + esc(c.timestamp) : 'thumbnail') : 'library') : (c.tags || []).map(esc).join(', ')}${(c.tags || []).includes('ai') ? ' · ✓ AI-verified' : ''}${offered.has(c.id) ? ' · offered' : ''}</div></div><div class="btns">${used ? `<button class="btn xs" data-rm="${esc(c.id)}">Remove</button>` : `<button class="btn xs tonal" data-hero="${esc(c.id)}">Hero</button><button class="btn xs" data-ins="${esc(c.id)}">Insert</button>`}</div></div>`; };
  const pl = PLACE.map((p, i) => `<div class="placement" data-i="${i}"><img src="/api/v1/images/${esc(p.image_id)}"><div><div class="overline" style="margin-bottom:6px">${p.placement === 'hero' ? 'Hero (featured image)' : 'Inline · appears where its [img=…] line is'}</div><input type="text" data-k="caption" value="${attr(p.caption || '')}" placeholder="caption"><input type="text" data-k="alt_text" value="${attr(p.alt_text || '')}" placeholder="alt text"></div><div class="stack" style="gap:6px">${p.placement !== 'hero' ? `<button class="btn xs tonal" data-mkhero="${i}">Make hero</button>` : ''}<button class="btn xs danger" data-rmi="${i}">Remove</button></div></div>`).join('');
  return `<div style="padding:20px 28px"><div class="row between"><span class="overline">In this article (${PLACE.length})</span><div class="row"><button class="btn sm filled" id="imgSave">Save pictures as v${d.version + 1}</button></div></div><div style="margin-top:10px">${pl || '<div class="muted body-s">No pictures yet — pick from the stills or the library below.</div>'}</div>
    <div class="row between" style="margin-top:22px"><span class="overline">Stills from the analysed video ${jobId ? '· ' + esc(jobId) : ''}</span>${jobId ? `<div class="row"><button class="btn xs tonal" data-frames="${esc(jobId)}" data-mode="ai" title="shot detection + Gemini describes each still; only usable ones are kept">${icon('spark', 's')}Index stills with AI</button><button class="btn xs" data-frames="${esc(jobId)}" data-mode="blocks" title="one quick frame per media / key-moment block, no AI">Quick frames</button></div>` : '<span class="muted body-s">draft has no anchoring video</span>'}</div>
    <div class="pics" style="margin-top:10px">${CANDS.map((c) => card(c, 'frame')).join('') || '<div class="muted body-s">No stills yet — extract them from the video.</div>'}</div>
    <div class="overline" style="margin-top:22px">Photo library (${LIB.length})</div>
    <div class="pics" style="margin-top:10px">${LIB.map((c) => card(c, 'lib')).join('') || '<div class="muted body-s">Library is empty — upload the media team\'s own photos, or import a folder under the allowed roots.</div>'}</div>
    <div class="card tonal" style="margin-top:18px"><div class="bd" style="padding:16px 18px"><div class="row gap16" style="align-items:flex-end"><div class="field grow"><label>Add a photo</label><input type="file" id="libFile" accept=".jpg,.jpeg,.png,.webp"><input type="text" id="libDesc" placeholder="what the photo shows (the writer uses this to pick it)"><input type="text" id="libTags" placeholder="tags, comma separated"></div><button class="btn tonal" id="libUp">${icon('upload')}Upload</button></div>
    <div class="row gap16" style="align-items:flex-end;margin-top:12px"><div class="field grow"><label>Import a folder</label><input type="text" id="libPath" placeholder="/path/under/NAS_ALLOWED_ROOTS (optional captions.txt: name<TAB>description)"></div><button class="btn" id="libImp">Import</button></div><div class="msg" id="libMsg" style="margin-top:6px"></div></div></div></div>`;
}
async function loadImagesFor(d) {
  const jobId = d.evidence && d.evidence.job_id; const offered = (d.evidence && d.evidence.offered_images) || [];
  try { const own = jobId ? await api('/images?job_id=' + jobId + '&kind=frame') : []; const extra = offered.length ? await api('/images?ids=' + offered.join(',')) : []; const seen = new Set(own.map((x) => x.id)); CANDS = own.concat(extra.filter((x) => x.kind === 'frame' && !seen.has(x.id))); LIB = await api('/images?kind=library'); } catch { CANDS = []; LIB = []; }
}
function readPlacements(v) { v.querySelectorAll('.placement').forEach((row) => { const i = +row.dataset.i; row.querySelectorAll('input').forEach((inp) => { PLACE[i][inp.dataset.k] = inp.value; }); }); }
function bindImagesTab(v, d) {
  const all = () => [...CANDS, ...LIB]; const rerender = () => { draft.images = PLACE; drawReader(); };
  v.querySelectorAll('[data-hero]').forEach((b) => b.onclick = () => { readPlacements(v); const c = all().find((x) => x.id === b.dataset.hero); PLACE = PLACE.filter((p) => p.placement !== 'hero'); PLACE.unshift({ image_id: c.id, placement: 'hero', caption: c.description || '', alt_text: c.description || '' }); rerender(); });
  v.querySelectorAll('[data-ins]').forEach((b) => b.onclick = () => { readPlacements(v); const c = all().find((x) => x.id === b.dataset.ins); PLACE.push({ image_id: c.id, placement: 'inline', caption: c.description || '', alt_text: c.description || '' }); rerender(); });
  v.querySelectorAll('[data-rm]').forEach((b) => b.onclick = () => { readPlacements(v); PLACE = PLACE.filter((p) => p.image_id !== b.dataset.rm); rerender(); });
  v.querySelectorAll('[data-rmi]').forEach((b) => b.onclick = () => { readPlacements(v); PLACE.splice(+b.dataset.rmi, 1); rerender(); });
  v.querySelectorAll('[data-mkhero]').forEach((b) => b.onclick = () => { readPlacements(v); const i = +b.dataset.mkhero; PLACE.forEach((p) => { if (p.placement === 'hero') p.placement = 'inline'; }); PLACE[i].placement = 'hero'; PLACE.sort((a, b2) => (a.placement === 'hero' ? -1 : 0) - (b2.placement === 'hero' ? -1 : 0)); rerender(); });
  v.querySelectorAll('[data-frames]').forEach((fr) => fr.onclick = async () => { fr.disabled = true; const label = fr.innerHTML; fr.innerHTML = fr.dataset.mode === 'ai' ? '<span class="spin"></span> looking at the video…' : '<span class="spin"></span> extracting…'; try { const r = await post('/jobs/' + fr.dataset.frames + '/frames?mode=' + fr.dataset.mode + '&force=' + (fr.dataset.mode === 'ai' ? 'true' : 'false')); await loadImagesFor(d); drawReader(); if (r.mode === 'blocks' && r.meta && r.meta.fallback_reason) toast('AI indexing unavailable (' + r.meta.fallback_reason + '); timestamp-based frames were extracted instead.', true); else toast(`${(r.images || r.records || []).length || ''} stills ready`.trim()); } catch (e) { toast(e.message, true); fr.disabled = false; fr.innerHTML = label; } });
  const save = v.querySelector('#imgSave'); if (save) save.onclick = async () => { readPlacements(v); save.disabled = true; try { const j = await put('/drafts/' + d.id + '/images', { images: PLACE }); toast('Pictures saved as v' + j.version); draft = null; tab = 'article'; draw(true); } catch (e) { toast(e.message, true); save.disabled = false; } };
  const up = v.querySelector('#libUp'); if (up) up.onclick = async () => { const f = v.querySelector('#libFile').files[0]; const m = v.querySelector('#libMsg'); if (!f) { m.textContent = 'Choose a photo.'; m.className = 'msg err'; return; } const fd = new FormData(); fd.append('file', f); fd.append('description', v.querySelector('#libDesc').value); fd.append('tags', v.querySelector('#libTags').value); up.disabled = true; try { const j = await upload('/images/library', fd); await loadImagesFor(d); drawReader(); toast('Added ' + j.source_name); } catch (e) { m.textContent = e.message; m.className = 'msg err'; up.disabled = false; } };
  const imp = v.querySelector('#libImp'); if (imp) imp.onclick = async () => { const p = v.querySelector('#libPath').value.trim(); const m = v.querySelector('#libMsg'); if (!p) { m.textContent = 'Enter a folder path.'; m.className = 'msg err'; return; } imp.disabled = true; try { const r = await post('/images/library/import', { path: p }); await loadImagesFor(d); drawReader(); toast('Imported ' + r.length + ' photo(s)'); } catch (e) { m.textContent = e.message; m.className = 'msg err'; imp.disabled = false; } };
}


// ---------------------------------------------------------------- publishing
async function drawPublished(el, d) {
  const box = el.querySelector('#pubs'); if (!box) return;
  let pubs = []; try { pubs = await api('/drafts/' + d.id + '/publications'); } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  box.innerHTML = (pubs.map((p) => `<div class="evb"><div class="row"><b>${esc(p.target_name)}</b><span class="tag ${p.status === 'published' ? 'ok' : p.status === 'failed' ? 'err' : p.status === 'draft' ? 'info' : 'pending'}">${esc(p.status)}${p.status === 'draft' ? ' on the site' : ''}</span><span class="muted body-s">v${p.draft_version ?? '–'} · ${esc(p.triggered_by)} · ${ago(p.updated_at)}</span><span class="sp"></span>${p.remote_url ? `<a class="btn xs" href="${attr(p.remote_url)}" target="_blank" rel="noopener">${icon('open', 's')}view</a>` : ''}${p.edit_url ? `<a class="btn xs" href="${attr(p.edit_url)}" target="_blank" rel="noopener">edit in WP</a>` : ''}</div>${p.error ? `<div class="banner err" style="margin-top:8px">${icon('warn')}<div class="mono">${esc(p.error)}</div></div>` : ''}${p.draft_version && p.draft_version < d.version ? `<div class="muted body-s" style="margin-top:6px">The site has v${p.draft_version}; this draft is v${d.version} — publish again to update it.</div>` : ''}</div>`).join('') || '<div class="empty">Not published anywhere yet.</div>')
    + `<div class="muted body-s" style="margin-top:10px">${d.status === 'approved' ? 'Use Publish above to send it to a website or update an earlier publication.' : 'Only approved articles can be published.'}</div>`;
  if (pubs.some((p) => p.status === 'queued' || p.status === 'publishing')) setTimeout(() => { if (tab === 'published' && draft && draft.id === d.id) drawPublished(el, d); }, 2500);
}

async function publishDialog(d) {
  let targets = []; try { targets = await api('/publish/targets'); } catch (e) { toast(e.message, true); return; }
  if (!targets.length) { targetDialog(null, () => publishDialog(d)); return; }
  let pubs = []; try { pubs = await api('/drafts/' + d.id + '/publications'); } catch { /* ignore */ }
  const done = Object.fromEntries(pubs.map((p) => [p.target_id, p]));
  openDialog(`<div class="dhd"><h3>Publish “${esc(d.title || d.brief)}”</h3><p>Pick the websites. A site that already has this article gets it updated (same post, pictures reused).</p></div>
    <div class="dbd">${targets.map((t) => `<label class="check" style="align-items:flex-start"><input type="checkbox" data-t="${t.id}" ${t.enabled ? 'checked' : ''}><span><b>${esc(t.name)}</b> <span class="muted body-s">${esc(t.url)} · default: ${t.mode === 'publish' ? 'live' : 'WP draft'}</span>${done[t.id] ? `<div class="muted body-s">already there: ${esc(done[t.id].status)} v${done[t.id].draft_version ?? '–'}</div>` : ''}</span></label>`).join('')}
      <div class="field"><label>How</label><select id="mode"><option value="">each site's own setting</option><option value="draft">as a WordPress draft (review on the site before it goes live)</option><option value="publish">publish live now</option></select></div>
      <div class="msg" id="m"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn" id="addsite">${icon('add')}Add website</button><button class="btn filled" id="go">${icon('send')}Publish</button></div>`, {
    onOpen(dlg) {
      dlg.querySelector('#addsite').onclick = () => { dlg.close(); targetDialog(null, () => publishDialog(d)); };
      dlg.querySelector('#go').onclick = async () => {
        const ids = [...dlg.querySelectorAll('[data-t]:checked')].map((x) => x.dataset.t); const m = dlg.querySelector('#m');
        if (!ids.length) { m.textContent = 'Pick at least one website.'; m.className = 'msg err'; return; }
        const mode = dlg.querySelector('#mode').value || null;
        if (mode === 'publish' || (mode === null && targets.some((t) => ids.includes(t.id) && t.mode === 'publish'))) { if (!(await confirmDialog({ title: 'Publish live?', body: 'The article becomes public on the selected website(s) immediately.', ok: 'Publish live' }))) return; }
        dlg.querySelector('#go').disabled = true;
        try { await post('/drafts/' + d.id + '/publish', { target_ids: ids, mode }); dlg.close(); toast('Publishing…'); tab = 'published'; drawReader(); }
        catch (e) { m.textContent = e.message; m.className = 'msg err'; dlg.querySelector('#go').disabled = false; }
      };
    },
  });
}

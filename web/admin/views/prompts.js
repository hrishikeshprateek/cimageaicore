// Prompts: read every prompt the platform sends to a model, save edits as new versions, switch the active one - live.
import { api, post, put, del, esc, attr, icon, ago, toast, confirmDialog, emptyState } from '../core.js';

export const title = 'Prompts';
export const subtitle = 'what we ask the model — versioned, editable, applied without a restart';
let root, ctx, data = null, kind = 'video-analysis', version = null, doc = null, dirty = false;

export async function render(el, params, c) { root = el; ctx = c; kind = params[0] || kind; version = params[1] || null; dirty = false; await load(); }
export function destroy() { root = null; }

async function load() {
  data = await api('/prompts');
  const k = data.kinds.find((x) => x.kind === kind) || data.kinds[0]; kind = k.kind;
  if (!version || !k.versions.some((v) => v.version === version)) version = k.active;
  doc = await api(`/prompts/${kind}/${encodeURIComponent(version)}`);
  draw();
}

function draw() {
  if (!root || !data) return;
  const k = data.kinds.find((x) => x.kind === kind);
  const list = data.kinds.map((x) => `<div class="li clickable ${x.kind === kind ? 'sel' : ''}" data-k="${x.kind}"><div class="avatar">${icon(x.sections ? 'edit_note' : 'article')}</div><div class="grow"><div class="t">${esc(x.label)}</div><div class="d"><span class="tag ${x.active !== x.default ? 'violet' : ''} mono">${esc(x.active)}</span> ${x.versions.length} version${x.versions.length === 1 ? '' : 's'}</div></div></div>`).join('');
  const ctxCard = `<div class="card"><div class="hd"><h3>Institution context</h3><span class="sp"></span><span class="muted body-s">fills {institution_context} everywhere</span></div><div class="bd"><textarea id="ictx" style="min-height:96px">${esc(data.institution_context)}</textarea><div class="row" style="margin-top:10px"><button class="btn filled sm" id="ictxSave">Apply</button>${data.institution_context !== data.institution_context_default ? `<button class="btn sm" id="ictxReset">Back to .env value</button>` : ''}<span class="muted body-s">used by the analyst, the writer, the shot picker and the studio</span></div></div></div>`;
  const chips = k.versions.map((v) => `<span class="chip ${v.version === version ? 'on' : ''}" data-v="${attr(v.version)}" title="${v.source} · ${ago(v.modified)}">${esc(v.version)}${v.version === k.active ? ' · active' : ''}${v.source === 'custom' ? ' <b>custom</b>' : ''}</span>`).join('');
  const isBundled = doc.source === 'bundled', isActive = doc.active;
  const editor = `<div class="card"><div class="hd" style="flex-wrap:wrap"><div><h3>${esc(k.label)}</h3><div class="muted body-s">${esc(k.used_by)}</div></div><span class="sp"></span><div class="chips" id="vchips">${chips}</div></div>
    <div class="bd">
      <div class="row between" style="margin-bottom:10px"><div class="row gap4"><span class="tag ${isBundled ? '' : 'violet'}">${isBundled ? 'bundled — read-only, save as a new version' : 'custom version'}</span>${isActive ? '<span class="tag ok">active</span>' : ''}</div>
        <div class="row">${!isActive ? `<button class="btn sm ok" id="activate">${icon('check', 's')}Make active</button>` : ''}${!isBundled && !isActive ? `<button class="btn sm danger" id="delete">${icon('trash', 's')}Delete</button>` : ''}</div></div>
      ${k.placeholders.length ? `<div class="muted body-s" style="margin-bottom:8px">Placeholders the code fills in: ${k.placeholders.map((p) => `<code>{${esc(p)}}</code>`).join(' ')}${k.sections ? ' · keep the <code>## system</code> and <code>## user</code> headings' : ''}</div>` : ''}
      <textarea id="ptext" class="mono" style="min-height:${k.sections ? 520 : 300}px;font-size:13px;line-height:1.55;white-space:pre-wrap">${esc(doc.text)}</textarea>
      <div id="pwarn" style="margin-top:8px">${warnHtml(doc.warnings)}</div>
      <div class="row" style="margin-top:12px;gap:10px;align-items:flex-end;flex-wrap:wrap">
        <div class="field" style="width:220px"><label>Save as version</label><input type="text" id="vname" value="${attr(isBundled ? doc.suggested_version : doc.version)}" placeholder="${attr(doc.suggested_version)}"></div>
        <label class="check" style="height:40px"><input type="checkbox" id="vact" checked> make it active</label>
        <button class="btn filled" id="save">${icon('check')}Save${isBundled ? ' as new version' : ''}</button>
        <span class="muted body-s" id="pmsg"></span></div>
    </div></div>`;
  root.innerHTML = `<div class="page-head"><div><h2>Prompts &amp; context</h2><p>Bundled versions ship with the code and never change; your edits become new versions in <code>${esc(data.overlay_dir)}</code>. Switching the active version takes effect on the next analysis / draft.</p></div></div>
    <div class="g2" style="grid-template-columns:320px minmax(0,1fr)"><div class="stack"><div class="card"><div class="bd flush list" id="klist">${list}</div></div>${ctxCard}</div><div id="editor">${editor}</div></div>`;
  root.querySelectorAll('#klist .li').forEach((n) => n.onclick = async () => { if (dirty && !(await confirmDialog({ title: 'Discard unsaved changes?', ok: 'Discard', danger: true }))) return; kind = n.dataset.k; version = null; dirty = false; history.replaceState(null, '', '#prompts/' + kind); load(); });
  root.querySelectorAll('#vchips .chip').forEach((c) => c.onclick = async () => { if (dirty && !(await confirmDialog({ title: 'Discard unsaved changes?', ok: 'Discard', danger: true }))) return; version = c.dataset.v; dirty = false; history.replaceState(null, '', `#prompts/${kind}/${encodeURIComponent(version)}`); load(); });
  const ta = root.querySelector('#ptext'); ta.oninput = () => { dirty = true; root.querySelector('#pwarn').innerHTML = warnHtml(localWarnings(k, ta.value)); };
  root.querySelector('#save').onclick = () => save(k, false);
  const act = root.querySelector('#activate'); if (act) act.onclick = async () => { try { await put(`/prompts/${kind}/active`, { version }); toast(`${k.label}: ${version} is now active`); ctx.refresh(); load(); } catch (e) { toast(e.message, true); } };
  const dl = root.querySelector('#delete'); if (dl) dl.onclick = async () => { if (!(await confirmDialog({ title: `Delete version ${version}?`, body: 'Only this custom file is removed; bundled versions are never touched.', ok: 'Delete', danger: true }))) return; try { await del(`/prompts/${kind}/${encodeURIComponent(version)}`); toast('Deleted'); version = null; load(); } catch (e) { toast(e.message, true); } };
  root.querySelector('#ictxSave').onclick = async () => { try { await put('/prompts/context', { institution_context: root.querySelector('#ictx').value }); toast('Institution context applied'); load(); } catch (e) { toast(e.message, true); } };
  const rs = root.querySelector('#ictxReset'); if (rs) rs.onclick = async () => { await put('/prompts/context', { institution_context: null }); toast('Back to the .env value'); load(); };
}

function localWarnings(k, text) {
  const w = k.placeholders.filter((p) => !text.includes('{' + p + '}')).map((p) => `placeholder {${p}} is not used`);
  if (k.sections) { if (!/^## system\s*$/m.test(text)) w.unshift('missing "## system" heading'); if (!/^## user\s*$/m.test(text)) w.unshift('missing "## user" heading'); }
  return w;
}
const warnHtml = (ws) => ws && ws.length ? `<div class="banner warn">${icon('info')}<div>${ws.map(esc).join('<br>')}</div></div>` : '';

async function save(k, overwrite) {
  const v = root.querySelector('#vname').value.trim(), text = root.querySelector('#ptext').value, activate = root.querySelector('#vact').checked, msg = root.querySelector('#pmsg');
  if (!v) { msg.textContent = 'Give the version a name.'; return; }
  msg.textContent = 'Saving…';
  try {
    const r = await post(`/prompts/${kind}`, { version: v, text, activate, overwrite });
    dirty = false; version = v; toast(`Saved ${k.label} as ${v}${activate ? ' and made it active' : ''}`); ctx.refresh(); load();
  } catch (e) {
    if (/already exists/.test(e.message) && !overwrite) { if (await confirmDialog({ title: `Overwrite custom version ${v}?`, body: e.message, ok: 'Overwrite' })) return save(k, true); msg.textContent = ''; return; }
    msg.textContent = e.message; toast(e.message, true);
  }
}

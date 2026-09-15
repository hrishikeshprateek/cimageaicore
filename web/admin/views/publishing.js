// Publishing: the websites an approved article goes to (WordPress via application passwords), and what was published where.
import { api, post, put, del, esc, attr, icon, ago, dt, toast, confirmDialog, openDialog, emptyState } from '../core.js';

export const title = 'Publishing';
export const subtitle = 'approved articles land on your websites — as drafts or live, per site';
let root, ctx, targets = [], pubs = [], sig = '';
const sigOf = () => JSON.stringify([targets.map((t) => [t.id, t.enabled, t.mode, t.last_test_ok, t.updated_at]), pubs.map((p) => [p.id, p.status, p.updated_at])]);

export async function render(el, params, c) { root = el; ctx = c; sig = ''; await draw(); }
export async function tick() { if (root) await draw(); }
export function destroy() { root = null; }

async function draw() {
  if (!(ctx.overview.content && ctx.overview.content.available)) { root.innerHTML = `<div class="card"><div class="bd">${emptyState('send', 'Publishing needs PostgreSQL', 'set DATABASE_URL')}</div></div>`; return; }
  [targets, pubs] = await Promise.all([api('/publish/targets'), api('/publish/publications?limit=100')]);
  const now = sigOf(); if (now === sig) return; sig = now;
  const live = targets.filter((t) => t.enabled).length, auto = targets.filter((t) => t.enabled && t.auto_on_approval).length;
  root.innerHTML = `<div class="page-head"><div><h2>Websites</h2><p>${targets.length ? `${live} enabled · ${auto} publish automatically when you approve an article` : 'Add a WordPress site to start publishing approved articles.'}</p></div><span class="sp"></span><button class="btn filled" id="add">${icon('add')}Add website</button></div>
  <div class="card" style="margin-bottom:20px"><div class="list">${targets.map((t) => `<div class="li"><div class="avatar">${icon('link')}</div><div class="grow"><div class="t">${esc(t.name)} <span class="muted body-s">${esc(t.url)}</span>${t.site_title ? `<span class="muted body-s"> · ${esc(t.site_title)}</span>` : ''}</div>
      <div class="d"><span class="tag ${t.enabled ? (t.mode === 'publish' ? 'ok' : 'info') : ''}">${t.enabled ? (t.mode === 'publish' ? 'publishes live' : 'creates WP drafts') : 'disabled'}</span><span>${t.auto_on_approval ? 'auto on approval' : 'manual only'}</span><span>·</span><span>user ${esc(t.username)}</span>${t.default_category ? `<span>·</span><span>category ${esc(t.default_category)}</span>` : ''}${t.seo_plugin !== 'none' ? `<span>·</span><span>${esc(t.seo_plugin)} SEO</span>` : ''}<span>·</span><span>${(t.layout || {}).image_position === 'as_placed' ? 'pictures as placed' : 'pictures under headings'}${(t.layout || {}).hero_in_body === false ? '' : ' · lead image'}${(t.layout || {}).image_captions ? ' · captions' : ''}</span><span>·</span><span>${t.last_test_at ? (t.last_test_ok ? `✓ connected ${ago(t.last_test_at)}` : `<span style="color:var(--g-red)">✗ ${esc(t.last_error || 'connection failed')}</span>`) : 'not tested yet'}</span></div></div>
      <div class="trail"><button class="btn xs" data-test="${t.id}">Test</button><button class="btn xs" data-edit="${t.id}">${icon('edit', 's')}Edit</button><button class="btn xs danger" data-del="${t.id}">${icon('trash', 's')}</button></div></div>`).join('') || emptyState('link', 'No websites yet', 'WordPress → Users → Profile → Application Passwords gives you the key')}</div></div>
  <div class="card"><div class="hd"><h3>Recent publications</h3><span class="sp"></span><span class="muted body-s">${pubs.length}</span></div><div class="bd flush tw"><table><thead><tr><th>Article</th><th>Website</th><th>Result</th><th>When</th><th></th></tr></thead><tbody>
    ${pubs.map((p) => `<tr><td><a href="#drafts/${esc(p.draft_id)}" class="mono">${esc(p.draft_id)}</a><div class="muted body-s">v${p.draft_version ?? '–'} · ${esc(p.triggered_by)}</div></td><td>${esc(p.target_name)}</td><td><span class="tag ${p.status === 'published' ? 'ok' : p.status === 'failed' ? 'err' : p.status === 'draft' ? 'info' : 'pending'}">${esc(p.status)}</span>${p.error ? `<div class="muted body-s" style="max-width:360px">${esc(p.error)}</div>` : ''}</td><td class="muted nowrap">${ago(p.updated_at)}</td><td class="right nowrap">${p.remote_url ? `<a class="btn xs" href="${attr(p.remote_url)}" target="_blank" rel="noopener">${icon('open', 's')}view</a> ` : ''}${p.edit_url ? `<a class="btn xs" href="${attr(p.edit_url)}" target="_blank" rel="noopener">edit in WP</a>` : ''}</td></tr>`).join('') || `<tr><td colspan="5">${emptyState('send', 'Nothing published yet', 'approve a draft and it appears here')}</td></tr>`}
  </tbody></table></div></div>`;
  root.querySelector('#add').onclick = () => targetDialog(null);
  root.querySelectorAll('[data-edit]').forEach((b) => b.onclick = () => targetDialog(targets.find((t) => t.id === b.dataset.edit)));
  root.querySelectorAll('[data-test]').forEach((b) => b.onclick = async () => { b.disabled = true; b.textContent = 'testing…'; try { const r = await post('/publish/targets/' + b.dataset.test + '/test'); toast(`Connected to ${r.site_title || 'the site'} as ${r.user}${r.can_publish ? '' : ' — this user cannot publish posts'}`); } catch (e) { toast(e.message, true); } sig = ''; draw(); });
  root.querySelectorAll('[data-del]').forEach((b) => b.onclick = async () => { const t = targets.find((x) => x.id === b.dataset.del); if (!(await confirmDialog({ title: `Remove ${t.name}?`, body: 'Articles already on the site stay there; the platform just forgets this website.', ok: 'Remove', danger: true }))) return; try { await del('/publish/targets/' + t.id); toast('Website removed'); } catch (e) { toast(e.message, true); } sig = ''; draw(); });
}

export function targetDialog(t, onSaved) {
  const v = (k, d = '') => attr(t ? (t[k] ?? d) : d);
  openDialog(`<div class="dhd"><h3>${t ? 'Edit website' : 'Add a WordPress website'}</h3><p>Create an <b>application password</b> in WordPress (Users → Profile → Application Passwords) for an editor/author account. It is stored encrypted and never shown again.</p></div>
    <div class="dbd">
      <div class="row gap16"><div class="field grow"><label>Name</label><input type="text" id="name" value="${v('name')}" placeholder="cimage.in"></div><div class="field grow"><label>Site URL</label><input type="url" id="url" value="${v('url')}" placeholder="https://cimage.in"></div></div>
      <div class="row gap16"><div class="field grow"><label>WordPress username</label><input type="text" id="user" value="${v('username')}"></div><div class="field grow"><label>Application password ${t ? '<span class="muted">(leave blank to keep)</span>' : ''}</label><input type="text" id="pw" autocomplete="off" placeholder="xxxx xxxx xxxx xxxx xxxx xxxx"></div></div>
      <div class="row gap16"><div class="field grow"><label>When an article is approved</label><select id="mode"><option value="draft" ${!t || t.mode === 'draft' ? 'selected' : ''}>create a WordPress draft (you press Publish on the site)</option><option value="publish" ${t && t.mode === 'publish' ? 'selected' : ''}>publish live immediately</option></select></div></div>
      <div class="row gap16"><div class="field grow"><label>Default category</label><input type="text" id="cat" value="${v('default_category')}" placeholder="News (created if missing)"></div><div class="field grow"><label>SEO plugin</label><select id="seo"><option value="none" ${!t || t.seo_plugin === 'none' ? 'selected' : ''}>none</option><option value="yoast" ${t && t.seo_plugin === 'yoast' ? 'selected' : ''}>Yoast SEO</option><option value="rankmath" ${t && t.seo_plugin === 'rankmath' ? 'selected' : ''}>Rank Math</option></select></div></div>
      <div class="row gap16"><label class="check"><input type="checkbox" id="auto" ${!t || t.auto_on_approval ? 'checked' : ''}> publish automatically on approval</label><label class="check"><input type="checkbox" id="enabled" ${!t || t.enabled ? 'checked' : ''}> enabled</label></div>
      <div class="overline" style="margin-top:4px">Article layout on this site <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">(defaults match cimage.in's posts)</span></div>
      <div class="row gap16"><label class="check"><input type="checkbox" id="heroBody" ${!t || (t.layout || {}).hero_in_body !== false ? 'checked' : ''}> lead image in the body after the intro</label><label class="check"><input type="checkbox" id="caps" ${t && (t.layout || {}).image_captions ? 'checked' : ''}> captions under pictures</label></div>
      <div class="field"><label>Section pictures</label><select id="imgpos"><option value="under_heading" ${!t || (t.layout || {}).image_position !== 'as_placed' ? 'selected' : ''}>directly under each section heading (house style)</option><option value="as_placed" ${t && (t.layout || {}).image_position === 'as_placed' ? 'selected' : ''}>where the writer placed them</option></select></div>
      <div class="msg" id="m"></div></div>
    <div class="dft"><button class="btn" data-close>Cancel</button><button class="btn filled" id="save">${icon('check')}${t ? 'Save' : 'Add & test connection'}</button></div>`, {
    onOpen(dlg) {
      dlg.querySelector('#save').onclick = async () => {
        const g = (id) => dlg.querySelector('#' + id); const m = g('m');
        const body = { name: g('name').value.trim(), url: g('url').value.trim(), username: g('user').value.trim(), mode: g('mode').value, default_category: g('cat').value.trim() || null, seo_plugin: g('seo').value, auto_on_approval: g('auto').checked, enabled: g('enabled').checked,
          layout: { hero_in_body: g('heroBody').checked, image_captions: g('caps').checked, image_position: g('imgpos').value } };
        const pw = g('pw').value.trim(); if (pw) body.app_password = pw;
        if (!body.name || !body.url || !body.username || (!t && !pw)) { m.textContent = 'Name, URL, username and application password are required.'; m.className = 'msg err'; return; }
        g('save').disabled = true; m.textContent = 'Saving…'; m.className = 'msg';
        try {
          const saved = t ? await put('/publish/targets/' + t.id, body) : await post('/publish/targets', body);
          m.textContent = 'Testing the connection…';
          try { const r = await post('/publish/targets/' + saved.id + '/test'); toast(`Connected to ${r.site_title || saved.name} as ${r.user}`); }
          catch (e) { toast('Saved, but the connection test failed: ' + e.message, true); }
          dlg.close(); if (onSaved) onSaved(saved); if (root) { sig = ''; draw(); }
        } catch (e) { m.textContent = e.message; m.className = 'msg err'; g('save').disabled = false; }
      };
    },
  });
}

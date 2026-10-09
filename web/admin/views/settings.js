import { api, put, post, esc, attr, icon, toast, confirmDialog } from '../core.js';

export const title = 'Settings';
export const subtitle = 'how speech becomes sentences, and how long a reel may run — saved live, no restart';
let root, ctx, data = null, edited = {};

export async function render(el, params, c) { root = el; ctx = c; edited = {}; await draw(); }

async function draw() {
  try { data = await api('/settings'); } catch (e) { root.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  root.innerHTML = `<div class="banner" style="margin-bottom:18px">${icon('tune')}
      Every reel cut ends on a sentence end. These settings decide where those sentence ends are, and how far a cut may
      run to reach one. A field showing <span class="tag">.env</span> is inherited from the server's configuration file;
      change it here and it becomes <span class="tag ok">set here</span>, stored in <code>data/runtime_settings.json</code>.
    </div>
    ${data.groups.map(group).join('')}
    <div class="row" style="gap:10px;margin-top:20px;align-items:center">
      <button class="btn filled" id="stSave" disabled>${icon('check')}Save and apply</button>
      <button class="btn" id="stRevert" disabled>Discard changes</button>
      <span class="muted body-s grow" id="stDirty"></span>
    </div>`;
  wire();
}

const group = (g) => `<div class="card" style="margin-bottom:18px">
  <div class="hd"><h3>${esc(g.title)}</h3>${g.key === 'transcription' ? `<span class="sp"></span><span class="tag ${data.transcribe_provider === 'off' ? 'err' : 'ok'}">provider: ${esc(data.transcribe_provider)}</span>` : ''}</div>
  <div class="bd">
    <div class="muted body-s" style="margin-bottom:16px">${esc(g.note)}</div>
    <div style="display:flex;flex-direction:column;gap:18px">${g.fields.map(field).join('')}</div>
    ${g.key === 'transcription' ? `<div class="row" style="gap:10px;margin-top:20px;padding-top:16px;border-top:1px solid var(--line)">
        <button class="btn tonal" id="stRetr">${icon('refresh')}Re-transcribe every video</button>
        <span class="muted body-s">Existing videos keep the word timings they were transcribed with. Run this after
        changing the settings above so their cuts are placed on the new sentences.</span>
      </div>` : ''}
  </div></div>`;

function field(f) {
  const tag = f.source === 'ui' ? '<span class="tag ok" title="set in this page">set here</span>'
    : '<span class="tag" title="inherited from the server\'s .env file">.env</span>';
  const head = `<div class="row" style="gap:8px;align-items:baseline"><label for="f-${attr(f.key)}" style="font-weight:500">${esc(f.label)}</label>${tag}
    ${f.source === 'ui' ? `<button class="btn xs" data-reset="${attr(f.key)}" title="go back to the .env value">reset</button>` : ''}</div>`;
  const help = `<div class="muted body-s" style="margin-top:6px;max-width:70ch">${esc(f.help)}</div>`;
  let input;
  if (f.type === 'bool') {
    input = `<label class="row" style="gap:8px;cursor:pointer"><input type="checkbox" id="f-${attr(f.key)}" data-k="${attr(f.key)}" ${f.value ? 'checked' : ''}>
      <span class="body-s">${f.value ? 'On' : 'Off'}</span></label>`;
  } else if (f.type === 'choice') {
    input = `<select id="f-${attr(f.key)}" data-k="${attr(f.key)}" style="max-width:52ch">${f.choices.map((c) =>
      `<option value="${attr(c.value)}" ${String(f.value ?? '') === String(c.value) ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}</select>`;
  } else if (f.type === 'text') {
    input = `<textarea id="f-${attr(f.key)}" data-k="${attr(f.key)}" rows="${f.rows || 3}" style="min-height:${(f.rows || 3) * 26}px">${esc(f.value ?? '')}</textarea>`;
  } else {
    const unit = f.type === 'seconds' ? ' <span class="muted body-s">seconds</span>' : '';
    input = `<div class="row" style="gap:8px;align-items:center"><input type="number" id="f-${attr(f.key)}" data-k="${attr(f.key)}"
      value="${attr(f.value ?? '')}" min="${f.min}" max="${f.max}" step="${f.step}" style="max-width:120px">${unit}</div>`;
  }
  return `<div class="field" style="gap:4px">${head}${input}${help}</div>`;
}

const valueOf = (el) => el.type === 'checkbox' ? el.checked : el.type === 'number' ? (el.value === '' ? null : Number(el.value)) : el.value;

function wire() {
  const save = root.querySelector('#stSave'), revert = root.querySelector('#stRevert'), dirty = root.querySelector('#stDirty');
  const mark = () => {
    const n = Object.keys(edited).length;
    save.disabled = revert.disabled = !n;
    dirty.textContent = n ? `${n} unsaved change${n === 1 ? '' : 's'}` : '';
  };
  root.querySelectorAll('[data-k]').forEach((el) => {
    const original = data.groups.flatMap((g) => g.fields).find((f) => f.key === el.dataset.k).value;
    const on = () => {
      const v = valueOf(el);
      if (String(v) === String(original ?? '')) delete edited[el.dataset.k]; else edited[el.dataset.k] = v;
      if (el.type === 'checkbox') el.nextElementSibling.textContent = el.checked ? 'On' : 'Off';
      mark();
    };
    el.addEventListener(el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'input', on);
  });
  revert.onclick = () => { edited = {}; draw(); };
  save.onclick = async () => {
    save.disabled = true;
    try { data = await put('/settings', { values: edited }); edited = {}; toast('Settings saved and applied'); draw(); }
    catch (e) { toast(e.message, true); save.disabled = false; }
  };
  root.querySelectorAll('[data-reset]').forEach((b) => b.onclick = async () => {
    try { data = await put('/settings', { values: {}, reset: [b.dataset.reset] }); edited = {}; toast('Back to the .env value'); draw(); }
    catch (e) { toast(e.message, true); }
  });
  const rt = root.querySelector('#stRetr');
  if (rt) rt.onclick = async () => {
    if (!(await confirmDialog({
      title: 'Re-transcribe every video?', ok: 'Re-transcribe',
      body: 'Each video is sent through the speech model again with the settings above. This runs in the background and '
          + 'costs nothing (the model is local), but it is CPU-heavy — expect it to take a while on a large library.',
    }))) return;
    rt.disabled = true;
    try { const r = await post('/transcripts/rerun'); toast(`Queued ${r.queued} video${r.queued === 1 ? '' : 's'} for re-transcription`); }
    catch (e) { toast(e.message, true); }
    rt.disabled = false;
  };
}

// One picture picker for the whole panel: stills from a video, the photo library, or a file from this computer.
// Used by the article editor (change a picture where it sits) and the script writer (b-roll for a scene).
import { api, upload, esc, attr, icon, openDialog } from './core.js';

export async function uploadPhoto(file, description) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('description', description || file.name.replace(/\.[a-z0-9]+$/i, '').replace(/[_-]+/g, ' ').trim());
  fd.append('tags', 'upload');
  return upload('/images/library', fd);
}

export async function loadPictures(jobId, extraIds = []) {
  try {
    const own = jobId ? await api('/images?job_id=' + encodeURIComponent(jobId) + '&kind=frame') : [];
    const extra = extraIds.length ? await api('/images?ids=' + extraIds.join(',')) : [];
    const seen = new Set(own.map((x) => x.id));
    return { stills: own.concat(extra.filter((x) => x.kind === 'frame' && !seen.has(x.id))), library: await api('/images?kind=library') };
  } catch { return { stills: [], library: [] }; }
}

/** pickImage({jobId, extraIds, currentId, heading, onPick}) - onPick(imageRecord) runs after the dialog closes. */
export function pickImage({ jobId = null, extraIds = [], currentId = null, heading = 'Pick a picture', pictures = null, onPick }) {
  const dlg = openDialog(`<div class="dhd"><h3>${esc(heading)}</h3><p>A still from the analysed video, a photo from the library, or one from this computer — never the open internet.</p></div>
    <div class="dbd pickbd">
      <label class="drop sm" id="pdrop"><input type="file" id="pfile" accept=".jpg,.jpeg,.png,.webp" hidden><b>${icon('upload', 's')} Upload from this computer</b>drag a photo here or click to choose one — it joins the photo library and is used right away</label>
      <input type="search" id="pq" placeholder="Search by what is in the picture, a tag or a timestamp…" autocomplete="off">
      <div id="pres"><div class="loading"><span class="spin"></span></div></div>
      <div class="msg" id="pmsg"></div></div>
    <div class="dft"><span class="muted body-s grow">One click on a picture puts it in place.</span><button class="btn" data-close>Cancel</button></div>`, { onOpen: init });
  dlg.classList.add('wide');

  async function init(dl) {
    const pics = pictures || await loadPictures(jobId, extraIds);
    const res = dl.querySelector('#pres'), q = dl.querySelector('#pq'), msg = dl.querySelector('#pmsg');
    const tile = (c) => `<div class="pic pick ${c.id === currentId ? 'cur' : ''}" data-pick="${attr(c.id)}" title="${attr(c.description || '')}"><img src="${attr(c.url)}" loading="lazy"><div class="b"><div class="ellipsis">${esc(c.description || '(no description)')}</div><div class="k">${c.kind === 'frame' ? (c.timestamp ? 'still · ' + esc(c.timestamp) : 'still') : esc((c.tags || []).filter((t) => t !== 'upload').join(', ') || 'library photo')}${c.id === currentId ? ' · in place now' : ''}</div></div></div>`;
    const hit = (c, needle) => !needle || `${c.description} ${(c.tags || []).join(' ')} ${c.timestamp || ''} ${c.source_name || ''}`.toLowerCase().includes(needle);
    const paint = () => {
      const needle = q.value.trim().toLowerCase();
      const stills = pics.stills.filter((c) => hit(c, needle)), lib = pics.library.filter((c) => hit(c, needle));
      res.innerHTML = `<div class="overline">Stills from the video (${stills.length})</div><div class="pics" style="margin:8px 0 18px">${stills.map(tile).join('') || '<div class="muted body-s">none — extract stills in the article\'s Pictures tab</div>'}</div>
        <div class="overline">Photo library (${lib.length})</div><div class="pics" style="margin-top:8px">${lib.map(tile).join('') || '<div class="muted body-s">empty — upload a photo above</div>'}</div>`;
      res.querySelectorAll('[data-pick]').forEach((n) => n.onclick = async () => { const c = [...pics.stills, ...pics.library].find((x) => x.id === n.dataset.pick); dl.close(); await onPick(c); });
    };
    q.oninput = paint; paint(); q.focus();

    const take = async (file) => {
      if (!file) return;
      msg.textContent = `Uploading ${file.name}…`; msg.className = 'msg';
      try { const rec = await uploadPhoto(file); dl.close(); await onPick(rec); }
      catch (e) { msg.textContent = e.message; msg.className = 'msg err'; }
    };
    const drop = dl.querySelector('#pdrop'), file = dl.querySelector('#pfile');
    file.onchange = () => take(file.files[0]);
    drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('over'));
    drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); take(e.dataTransfer.files[0]); });
  }
}

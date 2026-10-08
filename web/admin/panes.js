// Draggable splitters between the studio's panels, remembered per layout - the way an editing application behaves.
// The sizes live in CSS variables on the shell, so the grid tracks follow them and nothing has to be re-rendered.

const LIMITS = { src: [150, 520], insp: [180, 560], tl: [120, 620] };

function read(key) {
  try { return JSON.parse(localStorage.getItem('cimage.panes.' + key) || '{}'); } catch { return {}; }
}
function write(key, v) {
  try { localStorage.setItem('cimage.panes.' + key, JSON.stringify(v)); } catch { /* private window */ }
}

/** Apply the saved sizes and make every [data-split] handle draggable. `onResize` runs after each change. */
export function installSplitters(shell, { key = 'studio', onResize = () => {} } = {}) {
  if (!shell) return () => {};
  const saved = read(key);
  const set = (name, px) => {
    const [lo, hi] = LIMITS[name] || [120, 800];
    const v = Math.round(Math.max(lo, Math.min(hi, px)));
    shell.style.setProperty('--' + name + '-w', name === 'tl' ? '' : v + 'px');
    if (name === 'tl') shell.style.setProperty('--tl-h', v + 'px');
    saved[name] = v;
    return v;
  };
  for (const name of Object.keys(LIMITS)) if (saved[name]) set(name, saved[name]);

  const stop = [];
  shell.querySelectorAll('[data-split]').forEach((sp) => {
    const name = sp.dataset.split;
    const vertical = name !== 'tl';
    const down = (e) => {
      e.preventDefault();
      const start = vertical ? e.clientX : e.clientY;
      const pane = name === 'src' ? shell.querySelector('.pane.src')
        : name === 'insp' ? shell.querySelector('.pane.insp') : shell.querySelector('.nle-tl');
      if (!pane) return;
      const from = vertical ? pane.getBoundingClientRect().width : pane.getBoundingClientRect().height;
      sp.classList.add('on');
      document.body.classList.add(vertical ? 'col-resize' : 'row-resize');
      const move = (ev) => {
        const delta = (vertical ? ev.clientX : ev.clientY) - start;
        set(name, name === 'insp' || name === 'tl' ? from - delta : from + delta);
        onResize();
      };
      const up = () => {
        document.removeEventListener('pointermove', move);
        document.removeEventListener('pointerup', up);
        sp.classList.remove('on');
        document.body.classList.remove('col-resize', 'row-resize');
        write(key, saved);
        onResize();
      };
      document.addEventListener('pointermove', move);
      document.addEventListener('pointerup', up);
    };
    sp.addEventListener('pointerdown', down);
    sp.addEventListener('dblclick', () => {          // double-click a splitter to reset that panel
      delete saved[name];
      shell.style.removeProperty(name === 'tl' ? '--tl-h' : '--' + name + '-w');
      write(key, saved); onResize();
    });
    stop.push(() => sp.removeEventListener('pointerdown', down));
  });
  return () => stop.forEach((f) => f());
}

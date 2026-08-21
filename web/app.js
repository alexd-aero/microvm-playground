/* microvm playground — UI controller */
'use strict';

const MEM_STEPS = [128, 192, 256, 384, 512, 768, 1024, 1536, 2048, 3072, 4096, 8192];
const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = { host: null, vms: [], ttl: '1h', open: null, term: null, ws: null, fit: null };

/* ── helpers ────────────────────────────────────────────────────── */
const fmtMem  = (m) => (m >= 1024 ? (m / 1024 % 1 ? (m / 1024).toFixed(1) : m / 1024) + ' GiB' : m + ' MiB');
const escape  = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 4200);
  setTimeout(() => el.remove(), 4600);
}

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  const body = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) throw new Error((body && (body.detail || body.error)) || `HTTP ${r.status}`);
  return body;
}

/* ripple on any .ripple */
document.addEventListener('pointerdown', (e) => {
  const t = e.target.closest('.ripple');
  if (!t) return;
  const r = t.getBoundingClientRect(), d = Math.max(r.width, r.height);
  const s = document.createElement('span');
  s.className = 'rip';
  s.style.cssText = `width:${d}px;height:${d}px;left:${e.clientX - r.left - d / 2}px;top:${e.clientY - r.top - d / 2}px`;
  t.appendChild(s);
  setTimeout(() => s.remove(), 600);
});

/* ── create form ────────────────────────────────────────────────── */
function paintSlider(el) {
  const pct = (el.value - el.min) / (el.max - el.min) * 100;
  el.style.setProperty('--pct', pct + '%');
}

function currentSpec() {
  return {
    name: $('#vmName').value.trim(),
    vcpus: +$('#vcpu').value,
    mem_mib: MEM_STEPS[+$('#mem').value],
    disk_gb: +$('#disk').value,
    ttl: state.ttl,
  };
}

function updateForm() {
  const spec = currentSpec();
  $('#vcpuOut').textContent = spec.vcpus;
  $('#memOut').textContent = fmtMem(spec.mem_mib);
  $('#diskOut').textContent = spec.disk_gb + ' GB';
  [$('#vcpu'), $('#mem'), $('#disk')].forEach(paintSlider);

  const lean = spec.vcpus <= 2 && spec.mem_mib <= 512;
  const badge = $('#presetBadge');
  badge.textContent = spec.mem_mib >= 4096 ? 'heavy' : lean ? 'lean' : 'balanced';

  const fitIn16 = Math.floor(16384 / spec.mem_mib);
  const notes = [];
  if (spec.mem_mib < 256) notes.push('below 256 MiB, expect the OOM killer on anything with a runtime');
  else if (spec.mem_mib <= 512) notes.push('good for Go/Rust binaries, shells, small APIs');
  else if (spec.mem_mib <= 2048) notes.push('comfortable for Node or Python');
  else notes.push('room for builds, browsers, or a JVM');
  if (spec.disk_gb <= 2) notes.push('2 GB of disk fits Alpine plus modest deps');

  $('#summary').innerHTML =
    `<b>${spec.vcpus}</b> vCPU · <b>${fmtMem(spec.mem_mib)}</b> · <b>${spec.disk_gb} GB</b> disk` +
    ` · destroy after <b>${spec.ttl}</b><br>` +
    `≈ <b>${fitIn16}</b> of these per 16 GB host — ${notes.join('; ')}`;
}

async function launch() {
  const btn = $('#launchBtn');
  btn.disabled = true;
  btn.querySelector('span').textContent = 'Booting…';
  try {
    const vm = await api('/api/vms', { method: 'POST', body: JSON.stringify(currentSpec()) });
    toast(`${vm.name} up in ${vm.boot_ms} ms`, 'ok');
    $('#vmName').value = '';
    await refresh();
    openConsole(vm.id);
  } catch (e) {
    toast('Launch failed: ' + e.message, 'err');
    await refresh();
  } finally {
    btn.disabled = false;
    btn.querySelector('span').textContent = 'Launch playground';
  }
}

/* ── vm list ────────────────────────────────────────────────────── */
function cardHTML(vm) {
  const uptime = fmtDuration((Date.now() / 1000) - vm.created_at);
  const ttl = vm.expires_at ? fmtDuration(vm.expires_at - Date.now() / 1000) : 'never';
  const live = vm.state === 'running';
  return `
  <article class="card ${vm.state === 'error' ? 'is-error' : ''}" data-id="${vm.id}">
    <div class="card-head">
      <div class="card-name">
        <i class="dot ${live ? 'dot-live' : vm.state === 'error' ? 'dot-bad' : 'dot-warn'}"></i>
        <span>${escape(vm.name)}</span>
      </div>
      <span class="state ${vm.state}">${vm.state}</span>
    </div>
    <div class="specs">
      <span class="spec"><b>${vm.vcpus}</b> vCPU</span>
      <span class="spec"><b>${fmtMem(vm.mem_mib)}</b></span>
      <span class="spec"><b>${vm.disk_gb}</b> GB</span>
    </div>
    ${vm.error ? `<div class="card-err">${escape(vm.error)}</div>` : `
    <div class="meta">
      <div><em>address</em><span>${vm.ip || '—'}</span></div>
      <div><em>boot</em><span>${vm.boot_ms != null ? vm.boot_ms + ' ms' : '—'}</span></div>
      <div><em>uptime</em><span data-uptime="${vm.created_at}">${uptime}</span></div>
      <div><em>destroys in</em><span data-ttl="${vm.expires_at || ''}">${ttl}</span></div>
    </div>`}
    <div class="card-actions">
      <button class="btn btn-ghost btn-sm ripple" data-act="console" ${live ? '' : 'disabled'}>Console</button>
      <button class="btn btn-danger btn-sm ripple" data-act="destroy">Destroy</button>
    </div>
  </article>`;
}

/* Rebuilding the grid on every 3s poll would drop hover, focus and any text
   selection, so only redraw when something actually changed. The live counters
   are updated in place by tickTimers(). */
function signature(vms) {
  return vms.map((v) => [v.id, v.state, v.ip, v.boot_ms, v.expires_at, v.error,
                        v.terminal_url].join('~')).join('|');
}

function render(force) {
  const sig = signature(state.vms);
  if (!force && sig === render._sig) return;
  render._sig = sig;

  const grid = $('#vmGrid'), empty = $('#empty');
  grid.innerHTML = state.vms.map(cardHTML).join('');
  empty.hidden = state.vms.length > 0;
  $('#destroyAll').hidden = state.vms.length === 0;
  const running = state.vms.filter((v) => v.state === 'running').length;
  $('#countChip').textContent = `${running} running · ${state.vms.length} total`;
}

function tickTimers() {
  // Scoped to the grid: the TTL preset chips also carry a data-ttl attribute.
  const now = Date.now() / 1000;
  $$('#vmGrid [data-uptime]').forEach((el) => { el.textContent = fmtDuration(now - +el.dataset.uptime); });
  $$('#vmGrid [data-ttl]').forEach((el) => {
    el.textContent = el.dataset.ttl ? fmtDuration(+el.dataset.ttl - now) : 'never';
  });
}

async function refresh() {
  try {
    state.vms = await api('/api/vms');
    render();
  } catch (e) { /* server restarting; next poll retries */ }
}

/* ── host banner ────────────────────────────────────────────────── */
function renderHost(h) {
  state.host = h;
  const chip = $('#modeChip');
  const mock = h.mode === 'mock';
  const bad = mock || h.problems.length > 0;
  chip.className = 'chip ' + (bad ? 'warn' : 'ok');

  // Every backend gets an explicit case. Never let an unrecognised mode fall
  // through to a label claiming to be something else.
  let label;
  if (h.mode === 'qemu') {
    const ver = (h.qemu || '').match(/version\s+([\d.]+)/);
    label = 'qemu' + (ver ? ' ' + ver[1] : '') + (h.accel ? ' · ' + h.accel : '');
  } else if (h.mode === 'firecracker') {
    label = 'firecracker' + (h.firecracker ? ' ' + h.firecracker.replace(/^.*?(v[\d.]+).*$/, '$1') : '');
  } else if (h.mode === 'container') {
    const ver = (h.qemu || '').match(/([\d.]+)$/);   // docker/podman version
    label = 'container' + (ver ? ' · docker ' + ver[1] : '') + ' · native';
  } else if (h.mode === 'mock') {
    label = 'mock mode';
  } else {
    label = h.mode || 'unknown backend';
  }
  chip.innerHTML = `<i class="dot ${bad ? 'dot-warn' : 'dot-live'}"></i><span>${escape(label)}</span>`;

  // Reflect the server's real limits and defaults in the sliders.
  $('#vcpu').max = h.limits.vcpus[1];
  $('#disk').min = h.limits.disk_gb[0];
  $('#disk').max = h.limits.disk_gb[1];
  $('#vcpu').value = h.defaults.vcpus;
  $('#disk').value = h.defaults.disk_gb;
  const memIdx = MEM_STEPS.indexOf(h.defaults.mem_mib);
  if (memIdx >= 0) $('#mem').value = memIdx;
  updateForm();

  const foot = $('.foot-hint');
  if (foot) {
    // `ttyd --version` prints "ttyd version 1.7.7"; take the number, not the
    // word, or the footer proudly reads "ttyd version".
    const tv = (h.ttyd || '').match(/(\d+\.\d[\d.]*)/);
    foot.innerHTML = h.terminal === 'ttyd'
      ? `terminal: <code>ttyd${tv ? ' ' + escape(tv[1]) : ''}</code> · UTF-8 · truecolor`
      : 'terminal: <code>built-in</code> · UTF-8 · xterm-256color · truecolor';
  }

  const box = $('#warnings');
  const rows = h.problems.map((p) => `<div class="warn-item"><b>!</b><span>${escape(p)}</span></div>`)
    .concat((h.notes || []).map((n) => `<div class="info-item"><b>i</b><span>${escape(n)}</span></div>`));
  box.hidden = rows.length === 0;
  box.innerHTML = rows.join('');
}

/* ── console ────────────────────────────────────────────────────── */
function makeTerm() {
  const term = new Terminal({
    allowProposedApi: true,
    cursorBlink: true,
    cursorStyle: 'block',
    fontFamily: '"Cascadia Mono","JetBrains Mono","SF Mono",Consolas,"DejaVu Sans Mono",monospace',
    fontSize: 13.5,
    lineHeight: 1.2,
    letterSpacing: 0,
    scrollback: 10000,
    convertEol: false,
    macOptionIsMeta: true,
    theme: {
      // Pitch black, classic xterm ANSI palette. The one deviation is normal
      // blue: true classic is #0000ee, unreadable on black, so it is lifted.
      background: '#000000',
      foreground: '#e5e5e5',
      cursor: '#e5e5e5',
      cursorAccent: '#000000',
      selectionBackground: 'rgba(94,234,212,.28)',
      black: '#000000',  brightBlack: '#7f7f7f',
      red: '#cd0000',    brightRed: '#ff0000',
      green: '#00cd00',  brightGreen: '#00ff00',
      yellow: '#cdcd00', brightYellow: '#ffff00',
      blue: '#3b3bff',   brightBlue: '#5c5cff',
      magenta: '#cd00cd', brightMagenta: '#ff00ff',
      cyan: '#00cdcd',   brightCyan: '#00ffff',
      white: '#e5e5e5',  brightWhite: '#ffffff',
    },
  });

  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  const uni = new Unicode11Addon.Unicode11Addon();
  term.loadAddon(uni);
  term.unicode.activeVersion = '11';   // correct widths for CJK / emoji / ZWJ
  return { term, fit };
}

/* xterm's default DOM renderer repaints whole rows through the layout engine,
   which is what makes typing feel laggy. WebGL draws from a glyph atlas
   instead; canvas is the fallback, DOM the last resort. Must run after
   term.open(), since these need a live element. */
function attachRenderer(term) {
  try {
    const gl = new WebglAddon.WebglAddon();
    // The GPU context can be lost (driver reset, tab eviction) — fall back
    // rather than leaving a dead black rectangle.
    gl.onContextLoss(() => {
      gl.dispose();
      try { term.loadAddon(new CanvasAddon.CanvasAddon()); } catch (e) { /* DOM renderer */ }
    });
    term.loadAddon(gl);
    return 'webgl';
  } catch (e) {
    try { term.loadAddon(new CanvasAddon.CanvasAddon()); return 'canvas'; }
    catch (e2) { return 'dom'; }
  }
}

const TERM_PREF = 'mvmp.terminal';   // 'ask' | 'builtin' | 'ttyd'

function termPref() {
  try { return localStorage.getItem(TERM_PREF) || 'ask'; } catch (e) { return 'ask'; }
}
function setTermPref(v) {
  try { v === 'ask' ? localStorage.removeItem(TERM_PREF) : localStorage.setItem(TERM_PREF, v); }
  catch (e) { /* private mode: the choice just will not persist */ }
}

function openConsole(id) {
  const vm = state.vms.find((v) => v.id === id);
  if (!vm) return;
  state.open = id;

  // Only worth asking when there is a real choice. Without ttyd -- Windows,
  // or MVMP_TTYD_ENABLE=off -- there is exactly one terminal, and a dialog
  // offering one option is just an extra click.
  if (!vm.terminal_url) return showConsole(vm, 'builtin');

  const pref = termPref();
  if (pref === 'builtin' || pref === 'ttyd') return showConsole(vm, pref);

  $('#pickRemember').checked = false;
  $('#pickOverlay').hidden = false;
  state.pickFor = id;
}

function showConsole(vm, kind) {
  $('#pickOverlay').hidden = true;
  $('#overlay').hidden = false;
  $('#consoleName').textContent = vm.name;
  $('#consoleSpecs').innerHTML =
    `<span class="spec"><b>${vm.vcpus}</b> vCPU</span><span class="spec"><b>${fmtMem(vm.mem_mib)}</b></span><span class="spec">${vm.ip || ''}</span>`;

  const hasChoice = !!vm.terminal_url;
  $('#termSwitch').hidden = !hasChoice;
  $('#btnAskAgain').hidden = !hasChoice || termPref() === 'ask';
  $$('#termSwitch .seg-btn').forEach((b) =>
    b.classList.toggle('is-on', b.dataset.term === kind));

  state.kind = kind;
  if (kind === 'ttyd') openTtyd(vm); else openBuiltin(vm.id);
}

function openTtyd(vm) {
  const frame = $('#ttydFrame');
  $('#term').hidden = true;
  $('#termWrap').classList.add('is-ttyd');
  frame.hidden = false;
  // Reload even for the same playground, so Reconnect gives a fresh shell.
  frame.src = vm.terminal_url + '?t=' + Date.now();
  $('#btnClear').hidden = true;
  $('#consoleStatus').textContent = 'ttyd';
  $('#consoleDot').className = 'dot dot-live';
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  setTimeout(() => { try { frame.contentWindow.focus(); } catch (e) {} }, 200);
}

function openBuiltin(id) {
  $('#ttydFrame').hidden = true;
  $('#ttydFrame').removeAttribute('src');
  $('#termWrap').classList.remove('is-ttyd');
  $('#term').hidden = false;
  $('#btnClear').hidden = false;

  if (!state.term) {
    const made = makeTerm();
    state.term = made.term;
    state.fit = made.fit;
    state.term.open($('#term'));
    state.renderer = attachRenderer(state.term);

    let rzTimer = null;
    state.term.onData((d) => {
      if (state.ws && state.ws.readyState === 1) state.ws.send(new TextEncoder().encode(d));
    });
    state.term.onResize(({ rows, cols }) => {
      clearTimeout(rzTimer);
      rzTimer = setTimeout(() => {
        // Only on a genuine change: each of these writes a visible `stty` line
        // into the shell, so repeating an unchanged size is pure noise.
        if (state.ws && state.ws.readyState === 1 &&
            (rows !== state.sentRows || cols !== state.sentCols)) {
          state.sentRows = rows;
          state.sentCols = cols;
          state.ws.send(JSON.stringify({ type: 'resize', rows, cols }));
        }
      }, 350);
    });
    window.addEventListener('resize', () => { if (!$('#overlay').hidden) safeFit(); });
  }

  state.term.reset();
  safeFit();
  connectWS(id);
  setTimeout(() => state.term.focus(), 60);
}

function safeFit() {
  try { state.fit.fit(); } catch (e) { /* element not laid out yet */ }
}

function connectWS(id) {
  if (state.ws) { try { state.ws.close(); } catch (e) {} }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/vms/${id}/console`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  const status = $('#consoleStatus'), dot = $('#consoleDot');
  status.textContent = 'connecting…';

  ws.onopen = () => {
    status.textContent = 'connected';
    dot.className = 'dot dot-live';
    safeFit();
    // Push the real size immediately. The guest does try to size itself, but
    // that runs at *login* -- which happens during boot, with no browser
    // attached, so its cursor-position query goes unanswered and it stays at
    // 80x24. Leaving it there makes the guest wrap lines the browser does not,
    // which garbles anything that redraws with \r (curl progress, spinners).
    // One stty line on attach is a far smaller cost than permanently wrong
    // wrapping.
    state.sentRows = state.term.rows;
    state.sentCols = state.term.cols;
    ws.send(JSON.stringify({ type: 'resize', rows: state.term.rows, cols: state.term.cols }));
  };
  ws.onmessage = (ev) => {
    state.term.write(new Uint8Array(ev.data));   // raw bytes → xterm decodes UTF-8
  };
  ws.onclose = () => {
    if (state.ws !== ws) return;
    status.textContent = 'disconnected';
    dot.className = 'dot dot-bad';
  };
  ws.onerror = () => { status.textContent = 'connection error'; dot.className = 'dot dot-bad'; };
}

function closeConsole() {
  $('#overlay').hidden = true;
  $('#pickOverlay').hidden = true;
  // Dropping the src tears down ttyd's client, which ends its bridge process
  // -- otherwise a closed dialog would keep a shell (and its CPU) alive.
  const frame = $('#ttydFrame');
  frame.hidden = true;
  frame.removeAttribute('src');
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  state.open = null;
}

function usingTtyd() {
  return !$('#ttydFrame').hidden;
}

/* ── wiring ─────────────────────────────────────────────────────── */
function init() {
  ['#vcpu', '#mem', '#disk'].forEach((s) => $(s).addEventListener('input', updateForm));

  $$('#ttlChips .chip-btn').forEach((b) => b.addEventListener('click', () => {
    $$('#ttlChips .chip-btn').forEach((x) => { x.classList.remove('is-on'); x.setAttribute('aria-checked', 'false'); });
    b.classList.add('is-on');
    b.setAttribute('aria-checked', 'true');
    state.ttl = b.dataset.ttl;
    updateForm();
  }));

  $('#launchBtn').addEventListener('click', launch);
  $('#vmName').addEventListener('keydown', (e) => { if (e.key === 'Enter') launch(); });

  $('#vmGrid').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = btn.closest('.card').dataset.id;
    if (btn.dataset.act === 'console') return openConsole(id);
    btn.disabled = true;
    try {
      if (state.open === id) closeConsole();
      await api('/api/vms/' + id, { method: 'DELETE' });
      toast('Playground destroyed');
    } catch (err) { toast('Destroy failed: ' + err.message, 'err'); }
    await refresh();
  });

  $('#destroyAll').addEventListener('click', async () => {
    closeConsole();
    await Promise.all(state.vms.map((v) => api('/api/vms/' + v.id, { method: 'DELETE' }).catch(() => {})));
    toast('All playgrounds destroyed');
    refresh();
  });

  $$('#pickOverlay .pick').forEach((b) => b.addEventListener('click', () => {
    const kind = b.dataset.term;
    if ($('#pickRemember').checked) setTermPref(kind);
    const vm = state.vms.find((v) => v.id === state.pickFor);
    if (vm) showConsole(vm, kind);
  }));
  $('#pickOverlay').addEventListener('mousedown', (e) => {
    if (e.target === $('#pickOverlay')) { $('#pickOverlay').hidden = true; state.open = null; }
  });

  // Switching live beats re-asking: same playground, other terminal.
  $$('#termSwitch .seg-btn').forEach((b) => b.addEventListener('click', () => {
    const vm = state.vms.find((v) => v.id === state.open);
    if (!vm || state.kind === b.dataset.term) return;
    if (termPref() !== 'ask') setTermPref(b.dataset.term);   // keep a saved choice honest
    showConsole(vm, b.dataset.term);
  }));

  $('#btnAskAgain').addEventListener('click', () => {
    setTermPref('ask');
    $('#btnAskAgain').hidden = true;
    toast('Will ask which terminal next time');
  });

  $('#btnCloseConsole').addEventListener('click', closeConsole);
  $('#btnClear').addEventListener('click', () => {
    if (usingTtyd()) return;             // ttyd handles Ctrl+L natively
    if (state.term) { state.term.clear(); state.term.focus(); }
  });
  $('#btnReconnect').addEventListener('click', () => {
    if (!state.open) return;
    const vm = state.vms.find((v) => v.id === state.open);
    if (vm && vm.terminal_url) return openTtyd(vm);
    state.term.reset();
    connectWS(state.open);
  });
  $('#overlay').addEventListener('mousedown', (e) => { if (e.target === $('#overlay')) closeConsole(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('#pickOverlay').hidden) { $('#pickOverlay').hidden = true; state.open = null; return; }
    if (!$('#overlay').hidden) closeConsole();
  });

  updateForm();
  api('/api/host').then(renderHost).catch(() => toast('Cannot reach the server', 'err'));
  refresh();
  setInterval(refresh, 3000);
  setInterval(tickTimers, 1000);
}

document.addEventListener('DOMContentLoaded', init);

/* Debug handle: lets you poke at the terminal and VM list from devtools,
   e.g. __mvmp.term.buffer.active or __mvmp.vms */
window.__mvmp = state;

'use strict';
// Diskscape front end — no dependencies, everything is drawn on a <canvas>.

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (s) => document.querySelector(s);
const MAX_RINGS = 6;
const TAU = Math.PI * 2;

const S = {
  home: '~',
  root: null,      // scan root path
  cur: null,       // folder currently in view
  tree: null,      // viz tree for cur
  nodes: [],       // flattened tree
  byPath: new Map(),
  rects: [],       // treemap rects (for hit testing)
  view: new URLSearchParams(location.hash.slice(1)).get('view') || load('view', 'sunburst'),
  tab: 'contents',
  hover: null,
  anim: null,
  scanInfo: null,
  rootName: 'Startup Disk',
  canAct: true,     // Finder actions are macOS-only
};

function load(k, d) { try { return localStorage.getItem('ds.' + k) || d; } catch { return d; } }
function save(k, v) { try { localStorage.setItem('ds.' + k, v); } catch {} }

async function api(path, body) {
  let r;
  try {
    r = await fetch(path, {
      method: body ? 'POST' : 'GET',
      headers: { 'X-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    showStopped(); // network error: the server has gone away
    throw e;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---------- formatting ----------
function fmt(b) {
  if (b < 1000) return b + ' B';
  const u = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let i = -1;
  do { b /= 1000; i++; } while (b >= 1000 && i < u.length - 1);
  return (b >= 100 ? b.toFixed(0) : b >= 10 ? b.toFixed(1) : b.toFixed(2)) + ' ' + u[i];
}
const num = (n) => n.toLocaleString();
const pct = (a, b) => (b ? (a / b * 100) : 0);
const pctStr = (a, b) => { const p = pct(a, b); return (p >= 10 ? p.toFixed(0) : p >= 1 ? p.toFixed(1) : p.toFixed(2)) + '%'; };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const base = (p) => p === '/' ? S.rootName : p.split('/').filter(Boolean).pop() || p;
const tilde = (p) => p.startsWith(S.home) ? '~' + p.slice(S.home.length) : p;
const dark = () => matchMedia('(prefers-color-scheme: dark)').matches;
const ease = (t) => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

const ICON = {
  dir: '<svg viewBox="0 0 20 20"><path d="M2 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/></svg>',
  file: '<svg viewBox="0 0 20 20"><path d="M5 2h7l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zm7 1v4h4"/></svg>',
  reveal: '<svg viewBox="0 0 20 20"><path d="M8.5 3a5.5 5.5 0 0 1 4.4 8.8l4.2 4.2-1.4 1.4-4.2-4.2A5.5 5.5 0 1 1 8.5 3zm0 2a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z"/></svg>',
  trash: '<svg viewBox="0 0 20 20"><path d="M7 3h6l1 2h3v2H3V5h3zm-2 5h10l-1 9H6z"/></svg>',
  open: '<svg viewBox="0 0 20 20"><path d="M2 5a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z"/></svg>',
};

// ---------- colour ----------
// Hue follows a node's angular position, so a folder and its contents share a family
// of colours and the sunburst, treemap and list all agree.
function hsl(n) {
  if (n.other) return dark() ? [222, 8, 34] : [222, 10, 78];
  const mid = (n.f0 + n.f1) / 2;
  const h = (210 + mid * 320) % 360;
  const d = Math.max(0, n.depth - 1);
  const s = n.dir ? 72 - d * 4 : 50 - d * 3;
  const l = dark() ? 62 - d * 5.5 : 58 - d * 4;
  return [h, s, l];
}
function color(n, a = 1, dl = 0) {
  const [h, s, l] = hsl(n);
  return `hsla(${h.toFixed(1)},${s}%,${l + dl}%,${a})`;
}

// ---------- views ----------
function show(id) {
  for (const v of ['startView', 'scanView', 'resultView']) $('#' + v).hidden = v !== id;
}

async function init() {
  const st = await api('/api/status');
  S.home = st.home;
  S.canAct = st.platform === 'darwin';
  api('/api/volumes').then((v) => { if (v[0] && v[0].path === '/' && v[0].name !== '/') S.rootName = v[0].name; }).catch(() => {});
  if (st.state === 'scanning' || st.state === 'finalizing') return watchScan();
  if (st.state === 'done') return openResults(st);
  showStart();
}

async function showStart() {
  show('startView');
  $('#diskMeter').hidden = true;
  const vols = await api('/api/volumes');
  $('#volumes').innerHTML = vols.map((v) => {
    const p = pct(v.used, v.total);
    return `<button class="vol" data-path="${esc(v.path)}">
      <div class="donut" style="background:conic-gradient(var(--accent) 0 ${p}%, var(--bg3) 0)"><b>${p.toFixed(0)}%</b></div>
      <div class="vname">${esc(v.name)}</div>
      <div class="vmeta">${fmt(v.free)} free of ${fmt(v.total)}</div>
    </button>`;
  }).join('');
  const h = S.home;
  const quick = [['Home', h], ['Applications', '/Applications'], ['Library', h + '/Library'],
    ['Downloads', h + '/Downloads'], ['Developer', h + '/Library/Developer'], ['Caches', h + '/Library/Caches']];
  $('#quickFolders').innerHTML = quick.map(([n, p]) => `<button class="chip" data-path="${esc(p)}">${n}</button>`).join('');
}

async function startScan(path) {
  try {
    await api('/api/scan', { path });
    watchScan();
  } catch (e) {
    $('#pathInput').setCustomValidity(e.message);
    $('#pathInput').reportValidity();
    setTimeout(() => $('#pathInput').setCustomValidity(''), 2000);
  }
}

async function watchScan() {
  show('scanView');
  $('#diskMeter').hidden = true;
  for (;;) {
    const st = await api('/api/status');
    $('#pathInput').value = tilde(st.root || '');
    $('#scanBytes').textContent = fmt(st.bytes || 0);
    $('#scanFiles').textContent = num(st.files || 0);
    $('#scanDirs').textContent = num(st.dirs || 0);
    $('#scanRate').textContent = num(Math.round((st.files || 0) / Math.max(st.elapsed, .1)));
    $('#scanTime').textContent = st.elapsed.toFixed(0) + 's';
    $('#scanCurrent').textContent = st.state === 'finalizing' ? 'Adding it all up…' : '‎' + tilde(st.current || '');
    if (st.state === 'done') return openResults(st);
    if (st.state === 'cancelled' || st.state === 'idle') return showStart();
    await new Promise((r) => setTimeout(r, 200));
  }
}

async function openResults(st) {
  S.scanInfo = st;
  S.root = st.root;
  $('#pathInput').value = tilde(st.root);
  show('resultView');
  resize();
  await navigate(st.root);
}

// ---------- navigation ----------
async function navigate(path, opts = {}) {
  const old = S.cur;
  const oldNode = S.byPath.get(path);
  let from = null;
  // Remember where the new focus sits in the current picture, to zoom from it.
  if (oldNode && oldNode.dir && old && path.startsWith(old + (old.endsWith('/') ? '' : '/'))) {
    const r = S.rects.find((x) => x.n === oldNode);
    from = { dir: 'down', f0: oldNode.f0, f1: oldNode.f1, depth: oldNode.depth, rect: r && { ...r } };
  }
  const tree = await api(`/api/tree?path=${encodeURIComponent(path)}&depth=${MAX_RINGS}&k=150&minfrac=0.0012`);
  S.cur = path;
  setTree(tree);
  if (!from && old && old.startsWith(path) && old !== path) {
    const child = S.byPath.get(old) || ancestorIn(old);
    if (child) {
      const r = S.rects.find((x) => x.n === child);
      from = { dir: 'up', f0: child.f0, f1: child.f1, depth: child.depth, rect: r && { ...r } };
    }
  }
  S.anim = !opts.noAnim ? { from, start: performance.now(), dur: from ? 520 : 300 } : null;
  S.hover = null;
  hideTip();
  renderChrome(tree);
  renderPanel();
  requestDraw();
}

function ancestorIn(p) {
  while (p && p !== S.cur) {
    if (S.byPath.has(p)) return S.byPath.get(p);
    p = p.slice(0, p.lastIndexOf('/')) || '/';
  }
  return null;
}

function setTree(tree) {
  S.tree = tree;
  S.nodes = [];
  S.byPath = new Map();
  const walk = (n, f0, f1, depth, parent) => {
    Object.assign(n, { f0, f1, depth, parent });
    S.nodes.push(n);
    if (n.path) S.byPath.set(n.path, n);
    if (n.children) {
      const denom = Math.max(n.size, n.children.reduce((a, c) => a + c.size, 0)) || 1;
      let f = f0;
      for (const c of n.children) {
        const w = (f1 - f0) * c.size / denom;
        walk(c, f, f + w, depth + 1, n);
        f += w;
      }
    }
  };
  walk(tree, 0, 1, 0, null);
  layoutTreemap();
}

function up() {
  if (!S.cur || S.cur === S.root) return;
  const p = S.cur.slice(0, S.cur.lastIndexOf('/')) || '/';
  navigate(p.length < S.root.length ? S.root : p);
}

function renderChrome(tree) {
  // breadcrumbs
  const rel = S.cur === S.root ? [] : S.cur.slice(S.root.length).split('/').filter(Boolean);
  let acc = S.root;
  const parts = [`<button data-path="${esc(S.root)}">${esc(tilde(S.root) === '~' ? 'Home' : base(S.root))}</button>`];
  for (const r of rel) {
    acc = acc.endsWith('/') ? acc + r : acc + '/' + r;
    parts.push(`<i>›</i><button data-path="${esc(acc)}">${esc(r)}</button>`);
  }
  $('#crumbs').innerHTML = parts.join('');
  $('#crumbs').scrollLeft = 1e6;
  $('#upBtn').disabled = S.cur === S.root;

  // centre label + panel header
  const name = S.cur === S.root && tilde(S.root) === '~' ? 'Home' : base(S.cur);
  $('#center .c-name').textContent = name;
  $('#center .c-size').textContent = fmt(tree.size);
  $('#center .c-count').textContent = num(tree.count || 0) + ' items';
  $('#phName').textContent = name;
  $('#phName').title = S.cur;
  $('#phMeta').textContent = `${fmt(tree.size)} · ${num(tree.count || 0)} items · ${tilde(S.cur)}`;

  // disk meter
  if (tree.disk) {
    const d = tree.disk;
    $('#diskMeter').hidden = false;
    $('.dm-used').style.width = pct(d.used, d.total) + '%';
    $('.dm-scan').style.width = pct(tree.size, d.total) + '%';
    $('.dm-label').textContent = `${S.cur === S.root ? 'Scanned' : 'This folder'} ${fmt(tree.size)} · ${fmt(d.used)} used · ${fmt(d.free)} free`;
  }

  const st = S.scanInfo;
  $('#status').innerHTML = `<span>Scanned ${num(st.files)} files in ${num(st.dirs)} folders in ${st.elapsed.toFixed(1)}s</span>` +
    (st.errors ? `<span class="warn" title="Grant Full Disk Access to your terminal app in System Settings › Privacy & Security to include these.">⚠ ${num(st.errors)} items couldn't be read</span>` : '') +
    `<span style="margin-left:auto">Sizes are space on disk · right-click for actions</span>`;
}

// ---------- canvas ----------
const canvas = $('#chart');
const ctx = canvas.getContext('2d');
let W = 0, H = 0, DPR = 1, drawQueued = false;

function resize() {
  const r = $('#viz').getBoundingClientRect();
  DPR = window.devicePixelRatio || 1;
  W = r.width; H = r.height;
  canvas.width = Math.round(W * DPR);
  canvas.height = Math.round(H * DPR);
  if (S.tree) layoutTreemap();
  requestDraw();
}
new ResizeObserver(resize).observe($('#viz'));

function requestDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; draw(); });
}

function geom() {
  const R = Math.max(40, Math.min(W, H) / 2 - 14);
  const r0 = R * 0.27;
  const depth = Math.min(MAX_RINGS, S.nodes.reduce((m, n) => Math.max(m, n.depth), 1));
  return { cx: W / 2, cy: H / 2, R, r0, ring: (R - r0) / depth };
}

function animT() {
  if (!S.anim) return 1;
  const t = Math.min(1, (performance.now() - S.anim.start) / S.anim.dur);
  if (t >= 1) { S.anim = null; return 1; }
  requestDraw();
  return ease(t);
}

function draw() {
  if (!S.tree || !W) return;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const t = animT();
  const center = $('#center');
  if (S.view === 'sunburst') {
    drawSunburst(t);
    const g = geom();
    Object.assign(center.style, { display: '', left: g.cx - g.r0 + 'px', top: g.cy - g.r0 + 'px', width: g.r0 * 2 + 'px', height: g.r0 * 2 + 'px' });
  } else {
    center.style.display = 'none';
    drawTreemap(t);
  }
}

function lit(n) {
  const h = S.hover;
  if (!h) return true;
  for (let p = n; p; p = p.parent) if (p === h) return true;      // h or its descendants
  for (let p = h.parent; p; p = p.parent) if (p === n) return true; // ancestors of h
  return false;
}

function drawSunburst(t) {
  const { cx, cy, r0, ring, R } = geom();
  const a = S.anim && S.anim.from;
  let map = (x) => x, dOff = 0, alpha = 1;
  if (a && t < 1) {
    const span = a.f1 - a.f0 || 1;
    if (a.dir === 'down') {
      map = (x) => (a.f0 + x * span) * (1 - t) + x * t;
      dOff = (a.depth) * (1 - t);
    } else {
      map = (x) => ((x - a.f0) / span) * (1 - t) + x * t;
      dOff = -(a.depth) * (1 - t);
    }
  } else if (S.anim) {
    alpha = t;
  }

  // soft backdrop disc
  ctx.beginPath();
  ctx.arc(cx, cy, r0 - 3, 0, TAU);
  ctx.fillStyle = dark() ? 'rgba(255,255,255,.035)' : 'rgba(0,0,0,.035)';
  ctx.fill();

  const bg = getComputedStyle(document.body).backgroundColor;
  ctx.lineWidth = 1;
  ctx.strokeStyle = bg;
  ctx.lineJoin = 'round';
  for (const n of S.nodes) {
    if (n.depth < 1) continue;
    const d = n.depth + dOff;
    if (d < 0.999 || d > MAX_RINGS + 0.001) continue;
    let a0 = Math.max(0, Math.min(1, map(n.f0)));
    let a1 = Math.max(0, Math.min(1, map(n.f1)));
    if (a1 - a0 < 0.0009) continue;
    const ri = r0 + (d - 1) * ring;
    const ro = Math.min(R, ri + ring) - 0.5;
    if (ro <= ri) continue;
    const A0 = a0 * TAU - Math.PI / 2, A1 = a1 * TAU - Math.PI / 2;
    ctx.beginPath();
    ctx.arc(cx, cy, ro, A0, A1);
    ctx.arc(cx, cy, ri + 0.5, A1, A0, true);
    ctx.closePath();
    const on = lit(n);
    ctx.globalAlpha = alpha * (on ? 1 : 0.28);
    ctx.fillStyle = n === S.hover ? color(n, 1, 9) : color(n);
    ctx.fill();
    if (a1 - a0 > 0.002) ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // labels on large outer-enough arcs of the first ring
  if (t >= 1) {
    ctx.font = '600 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (const n of S.nodes) {
      if (n.depth !== 1 || n.other) continue;
      const span = (n.f1 - n.f0) * TAU;
      const rm = r0 + ring / 2;
      if (span * rm < 60 || ring < 18) continue;
      const am = (n.f0 + n.f1) / 2 * TAU - Math.PI / 2;
      const x = cx + Math.cos(am) * rm, y = cy + Math.sin(am) * rm;
      let rot = am + Math.PI / 2;
      if (rot > Math.PI / 2 && rot < Math.PI * 1.5) rot += Math.PI;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(rot);
      ctx.globalAlpha = lit(n) ? 1 : 0.3;
      ctx.fillStyle = textOn(n);
      ctx.fillText(clip(n.name, span * rm - 14), 0, 0);
      ctx.restore();
    }
  }
}

function textOn(n) {
  const l = hsl(n)[2];
  return l > 48 ? 'rgba(10,12,18,.88)' : 'rgba(255,255,255,.92)';
}

function clip(s, w) {
  if (ctx.measureText(s).width <= w) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const m = (lo + hi + 1) >> 1;
    if (ctx.measureText(s.slice(0, m) + '…').width <= w) lo = m; else hi = m - 1;
  }
  return lo ? s.slice(0, lo) + '…' : '';
}

// ---------- treemap ----------
function worst(row, sum, side) {
  let mx = 0, mn = Infinity;
  for (const r of row) { if (r.a > mx) mx = r.a; if (r.a < mn) mn = r.a; }
  const s2 = side * side, sum2 = sum * sum;
  return Math.max(s2 * mx / sum2, sum2 / (s2 * mn));
}

function squarify(kids, x, y, w, h) {
  const total = kids.reduce((a, c) => a + c.size, 0);
  const out = [];
  if (!total || w <= 1 || h <= 1) return out;
  const scale = w * h / total;
  const items = kids.filter((k) => k.size > 0).map((k) => ({ n: k, a: k.size * scale }));
  let i = 0;
  while (i < items.length) {
    const side = Math.min(w, h);
    const row = [items[i]];
    let sum = items[i].a, best = worst(row, sum, side);
    i++;
    while (i < items.length) {
      row.push(items[i]);
      const w2 = worst(row, sum + items[i].a, side);
      if (w2 > best) { row.pop(); break; }
      sum += items[i].a; best = w2; i++;
    }
    if (w >= h) {
      const cw = sum / h;
      let yy = y;
      for (const it of row) { const hh = it.a / cw; out.push({ n: it.n, x, y: yy, w: cw, h: hh }); yy += hh; }
      x += cw; w -= cw;
    } else {
      const rh = sum / w;
      let xx = x;
      for (const it of row) { const ww = it.a / rh; out.push({ n: it.n, x: xx, y, w: ww, h: rh }); xx += ww; }
      y += rh; h -= rh;
    }
  }
  return out;
}

function layoutTreemap() {
  S.rects = [];
  if (!S.tree || !W) return;
  const rec = (n, x, y, w, h, depth) => {
    const head = n.children && w > 70 && h > 44 ? 19 : 0;
    S.rects.push({ n, x, y, w, h, depth, head });
    if (!n.children || depth >= 4 || w < 26 || h < 26) return;
    const pad = 3;
    for (const r of squarify(n.children, x + pad, y + (head || pad), w - pad * 2, h - (head || pad) - pad)) {
      rec(r.n, r.x, r.y, r.w, r.h, depth + 1);
    }
  };
  const pad = 8;
  for (const r of squarify(S.tree.children || [], pad, pad, W - pad * 2, H - pad * 2)) rec(r.n, r.x, r.y, r.w, r.h, 1);
}

function drawTreemap(t) {
  const a = S.anim && S.anim.from;
  ctx.save();
  if (a && a.rect && t < 1) {
    // zoom between the focus rect and the full canvas
    const r = a.rect;
    const [sx, sy, tx, ty] = a.dir === 'down'
      ? [r.w / W, r.h / H, r.x, r.y]
      : [W / r.w, H / r.h, -r.x * W / r.w, -r.y * H / r.h];
    const k = 1 - t;
    const scx = sx * k + t, scy = sy * k + t;
    ctx.setTransform(DPR * scx, 0, 0, DPR * scy, DPR * tx * k, DPR * ty * k);
  }
  const dk = dark();
  ctx.textBaseline = 'middle';
  for (const r of S.rects) {
    const { n, x, y, w, h, head } = r;
    if (w < 0.5 || h < 0.5) continue;
    const on = lit(n);
    ctx.globalAlpha = (S.anim && !(a && a.rect) ? t : 1) * (on ? 1 : 0.35);
    const hv = n === S.hover;
    if (n.children) {
      ctx.fillStyle = color(n, 1, dk ? -22 : -14);
      roundRect(x, y, w, h, 5);
      ctx.fill();
      if (hv) { ctx.strokeStyle = color(n, 1, 22); ctx.lineWidth = 2; ctx.stroke(); }
      if (head) {
        ctx.font = '600 11.5px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillStyle = 'rgba(255,255,255,.92)';
        const sz = fmt(n.size);
        const sw = ctx.measureText(sz).width;
        ctx.fillText(clip(n.name, w - sw - 22), x + 7, y + 10);
        ctx.fillStyle = 'rgba(255,255,255,.6)';
        ctx.textAlign = 'right';
        if (w - sw > 60) ctx.fillText(sz, x + w - 7, y + 10);
      }
    } else {
      const g = ctx.createLinearGradient(x, y, x + w, y + h);
      g.addColorStop(0, color(n, 1, hv ? 16 : 8));
      g.addColorStop(1, color(n, 1, hv ? 2 : -6));
      ctx.fillStyle = g;
      roundRect(x + 0.5, y + 0.5, w - 1, h - 1, Math.min(4, w / 4, h / 4));
      ctx.fill();
      if (w > 46 && h > 30) {
        ctx.fillStyle = textOn(n);
        ctx.textAlign = 'left';
        ctx.font = '600 11.5px -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.fillText(clip(n.name, w - 14), x + 7, y + 13);
        if (h > 44) {
          ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
          ctx.globalAlpha *= 0.75;
          ctx.fillText(clip(fmt(n.size), w - 14), x + 7, y + 28);
        }
      }
    }
  }
  ctx.restore();
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, Math.max(0, r));
  else ctx.rect(x, y, w, h);
}

// ---------- hit testing & interaction ----------
function hit(ev) {
  const b = canvas.getBoundingClientRect();
  const x = ev.clientX - b.left, y = ev.clientY - b.top;
  if (S.view === 'sunburst') {
    const { cx, cy, r0, ring } = geom();
    const dx = x - cx, dy = y - cy, r = Math.hypot(dx, dy);
    if (r < r0) return null;
    const d = Math.floor((r - r0) / ring) + 1;
    let f = (Math.atan2(dy, dx) + Math.PI / 2) / TAU;
    if (f < 0) f += 1;
    return S.nodes.find((n) => n.depth === d && f >= n.f0 && f < n.f1) || null;
  }
  for (let i = S.rects.length - 1; i >= 0; i--) {
    const r = S.rects[i];
    if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) return r.n;
  }
  return null;
}

function setHover(n) {
  if (n === S.hover) return;
  S.hover = n;
  document.querySelectorAll('.row.hl').forEach((r) => r.classList.remove('hl'));
  if (n) {
    // highlight the row for this node or its top-level ancestor
    let p = n;
    while (p && p.depth > 1) p = p.parent;
    if (p && p.path) document.querySelector(`.row[data-path="${CSS.escape(p.path)}"]`)?.classList.add('hl');
  }
  requestDraw();
}

canvas.addEventListener('mousemove', (ev) => {
  if (S.anim) return;
  const n = hit(ev);
  setHover(n);
  canvas.style.cursor = n && n.dir ? 'pointer' : 'default';
  if (n) showTip(n, ev); else hideTip();
});
canvas.addEventListener('mouseleave', () => { setHover(null); hideTip(); });
canvas.addEventListener('click', (ev) => {
  const n = hit(ev);
  if (!n || n.other) return;
  if (n.dir) navigate(n.path);
  else flashRow(n.path);
});
canvas.addEventListener('contextmenu', (ev) => {
  const n = hit(ev);
  if (!n || n.other) return;
  ev.preventDefault();
  openMenu(n, ev.clientX, ev.clientY);
});

function showTip(n, ev) {
  const tip = $('#tip');
  const b = $('#viz').getBoundingClientRect();
  const rows = [`<div class="t-row"><b>${fmt(n.size)}</b> · ${pctStr(n.size, S.tree.size)} of ${esc(base(S.cur))}</div>`];
  if (n.dir && n.count != null) rows.push(`<div class="t-row">${num(n.count)} items</div>`);
  if (n.path) rows.push(`<div class="t-row">${esc(tilde(n.path))}</div>`);
  const hint = n.other ? '' : n.dir ? 'Click to open · right-click for actions' : 'Right-click to reveal or trash';
  tip.innerHTML = `<div class="t-name"><i style="background:${color(n)}"></i>${n.dir ? ICON.dir.replace('<svg', '<svg width="13" height="13" style="fill:#6f9bff"') : ''}${esc(n.name)}</div>${rows.join('')}${hint ? `<div class="t-hint">${hint}</div>` : ''}`;
  tip.hidden = false;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = ev.clientX - b.left + 16, y = ev.clientY - b.top + 16;
  if (x + tw > b.width - 8) x = ev.clientX - b.left - tw - 16;
  if (y + th > b.height - 8) y = ev.clientY - b.top - th - 16;
  tip.style.left = Math.max(8, x) + 'px';
  tip.style.top = Math.max(8, y) + 'px';
}
function hideTip() { $('#tip').hidden = true; }

// ---------- side panel ----------
let listCache = {};

async function renderPanel() {
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === S.tab));
  const list = $('#list');
  const key = S.cur + '|' + S.tab;
  let data = listCache.key === key ? listCache.data : null;
  if (!data) {
    list.innerHTML = '<div class="empty">Loading…</div>';
    const p = encodeURIComponent(S.cur);
    data = S.tab === 'contents' ? await api(`/api/tree?path=${p}&depth=1&k=1000&minfrac=0`)
      : S.tab === 'largest' ? await api(`/api/top?path=${p}&n=200`)
      : await api(`/api/types?path=${p}`);
    if (S.cur + '|' + S.tab !== key) return; // navigated away meanwhile
    listCache = { key, data };
  }
  if (S.tab === 'types') return renderTypes(data);
  const items = S.tab === 'contents' ? data.children || [] : data;
  const total = S.tree.size || 1;
  const max = items.reduce((m, c) => Math.max(m, c.size), 0) || 1;
  if (!items.length) { list.innerHTML = '<div class="empty">This folder is empty.</div>'; return; }
  list.innerHTML = items.map((c) => {
    const vn = c.path && S.byPath.get(c.path);
    const col = vn ? color(vn) : c.other ? color({ other: true }) : 'var(--muted)';
    const sub = S.tab === 'largest' ? `<span class="sub2">‎${esc(tilde(c.path.slice(0, c.path.lastIndexOf('/'))))}</span>` : '';
    const acts = c.other || !S.canAct ? '' : `<div class="acts">
        <button data-act="reveal" title="Reveal in Finder" aria-label="Reveal in Finder">${ICON.reveal}</button>
        <button data-act="trash" class="del" title="Move to Trash" aria-label="Move to Trash">${ICON.trash}</button></div>`;
    return `<div class="row${c.dir ? ' dir' : ''}${c.other ? ' other' : ''}" ${c.path ? `data-path="${esc(c.path)}"` : ''} data-dir="${c.dir ? 1 : 0}">
      <span class="sw" style="background:${col}"></span>
      <div class="nm">${c.other ? '' : c.dir ? ICON.dir : ICON.file}<div style="min-width:0"><span>${esc(c.name)}</span>${sub}</div></div>
      <div><span class="sz" title="${pctStr(c.size, total)}">${fmt(c.size)}</span>${acts}</div>
      <div class="bar"><i style="width:${(c.size / max * 100).toFixed(2)}%;background:${col}"></i></div>
    </div>`;
  }).join('');
}

const CAT_COLORS = { Video: '#ff6f91', Images: '#ffb547', Audio: '#c77dff', Archives: '#ff8a4c', Code: '#4fd1a5', Documents: '#5b8cff', Data: '#3ab8ff', Other: '#8a92a6' };

function renderTypes(data) {
  const total = data.cats.reduce((a, c) => a + c.size, 0) || 1;
  const max = data.exts.reduce((m, c) => Math.max(m, c.size), 0) || 1;
  $('#list').innerHTML = `<div class="types-top">
      <div class="stack">${data.cats.map((c) => `<i title="${c.cat}: ${fmt(c.size)}" style="flex:${c.size} 0 0;background:${CAT_COLORS[c.cat]};min-width:2px"></i>`).join('')}</div>
      <div class="legend">${data.cats.map((c) => `<span><i style="background:${CAT_COLORS[c.cat]}"></i><b>${c.cat}</b><em>${fmt(c.size)} · ${pctStr(c.size, total)}</em></span>`).join('')}</div>
    </div>
    <div class="section-label">By extension</div>` +
    data.exts.map((e) => `<div class="row">
      <span class="sw" style="background:${CAT_COLORS[e.cat]}"></span>
      <div class="nm"><span class="ext-tag">${e.ext ? '.' + esc(e.ext) : 'no extension'}</span><span style="color:var(--muted);font-size:11.5px">${num(e.count)} files</span></div>
      <div><span class="sz">${fmt(e.size)}</span></div>
      <div class="bar"><i style="width:${(e.size / max * 100).toFixed(2)}%;background:${CAT_COLORS[e.cat]}"></i></div>
    </div>`).join('');
}

function flashRow(path) {
  if (S.tab !== 'contents') return;
  const row = document.querySelector(`.row[data-path="${CSS.escape(path)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  row.animate([{ background: 'color-mix(in srgb, var(--accent) 30%, transparent)' }, { background: 'transparent' }], { duration: 900 });
}

$('#list').addEventListener('click', (ev) => {
  const row = ev.target.closest('.row[data-path]');
  if (!row) return;
  const act = ev.target.closest('[data-act]')?.dataset.act;
  const path = row.dataset.path;
  if (act === 'reveal') return reveal(path);
  if (act === 'trash') return askTrash(path, row.querySelector('.nm span').textContent, row.querySelector('.sz').textContent);
  if (row.dataset.dir === '1') navigate(path);
});
$('#list').addEventListener('contextmenu', (ev) => {
  const row = ev.target.closest('.row[data-path]');
  if (!row) return;
  ev.preventDefault();
  openMenu({ path: row.dataset.path, dir: row.dataset.dir === '1', name: row.querySelector('.nm span').textContent,
    size: null, sizeText: row.querySelector('.sz').textContent }, ev.clientX, ev.clientY);
});
$('#list').addEventListener('mouseover', (ev) => {
  const row = ev.target.closest('.row[data-path]');
  const n = row && S.byPath.get(row.dataset.path);
  if (n !== S.hover) { S.hover = n || null; requestDraw(); }
});
$('#list').addEventListener('mouseleave', () => { S.hover = null; requestDraw(); });

// ---------- actions ----------
function openMenu(n, x, y) {
  const m = $('#menu');
  const sizeText = n.sizeText || fmt(n.size);
  m.innerHTML = `<div class="m-title">${esc(n.name)} · ${sizeText}</div>` +
    (n.dir ? `<button data-act="open">${ICON.open}Open</button>` : '') +
    (S.canAct ? `<button data-act="reveal">${ICON.reveal}Reveal in Finder</button>
     <button data-act="trash" class="del">${ICON.trash}Move to Trash…</button>` : '');
  m.hidden = false;
  m.style.left = Math.min(x, innerWidth - m.offsetWidth - 8) + 'px';
  m.style.top = Math.min(y, innerHeight - m.offsetHeight - 8) + 'px';
  m.onclick = (ev) => {
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (!act) return;
    m.hidden = true;
    if (act === 'open') navigate(n.path);
    if (act === 'reveal') reveal(n.path);
    if (act === 'trash') askTrash(n.path, n.name, sizeText);
  };
  hideTip();
}
document.addEventListener('mousedown', (ev) => { if (!ev.target.closest('#menu')) $('#menu').hidden = true; });

async function reveal(path) {
  try { await api('/api/reveal', { path }); } catch (e) { console.error(e); }
}

function askTrash(path, name, sizeText) {
  const dlg = $('#confirm');
  $('#cfText').innerHTML = `<b>${esc(name)}</b> (${esc(sizeText)}) will be moved to the Trash. You can put it back from the Trash in Finder.<br><br><span style="font-size:11.5px">${esc(tilde(path))}</span>`;
  $('#cfErr').textContent = '';
  $('#cfGo').disabled = false;
  dlg.showModal();
  dlg.onclose = null;
  $('#cfGo').onclick = async (ev) => {
    ev.preventDefault();
    $('#cfGo').disabled = true;
    try {
      await api('/api/trash', { path });
      dlg.close();
      listCache = {};
      const target = S.cur === path || S.cur.startsWith(path + '/') ? path.slice(0, path.lastIndexOf('/')) || '/' : S.cur;
      navigate(target, { noAnim: true });
    } catch (e) {
      $('#cfErr').textContent = e.message;
      $('#cfGo').disabled = false;
    }
  };
}

// ---------- wiring ----------
$('#scanForm').addEventListener('submit', (ev) => {
  ev.preventDefault();
  const v = $('#pathInput').value.trim() || '~';
  startScan(v);
});
$('#volumes').addEventListener('click', (ev) => { const b = ev.target.closest('[data-path]'); if (b) startScan(b.dataset.path); });
$('#quickFolders').addEventListener('click', (ev) => { const b = ev.target.closest('[data-path]'); if (b) startScan(b.dataset.path); });
$('#cancelBtn').addEventListener('click', async () => { await api('/api/cancel', {}); });
$('#upBtn').addEventListener('click', up);
$('#center').addEventListener('click', up);
$('#rescanBtn').addEventListener('click', () => { listCache = {}; startScan(S.root); });
$('#crumbs').addEventListener('click', (ev) => { const b = ev.target.closest('[data-path]'); if (b && b.dataset.path !== S.cur) navigate(b.dataset.path); });
$('#viewSeg').addEventListener('click', (ev) => {
  const v = ev.target.closest('[data-view]')?.dataset.view;
  if (!v || v === S.view) return;
  S.view = v; save('view', v);
  syncSeg();
  S.anim = { from: null, start: performance.now(), dur: 250 };
  requestDraw();
});
$('#tabs').addEventListener('click', (ev) => {
  const t = ev.target.closest('[data-tab]')?.dataset.tab;
  if (t && t !== S.tab) { S.tab = t; renderPanel(); }
});
function syncSeg() { document.querySelectorAll('#viewSeg button').forEach((b) => b.classList.toggle('on', b.dataset.view === S.view)); }
syncSeg();

document.addEventListener('keydown', (ev) => {
  if (ev.target.matches('input')) return;
  if (ev.key === 'Escape' && !$('#menu').hidden) { $('#menu').hidden = true; return; }
  if ((ev.key === 'Escape' || ev.key === 'Backspace') && !$('#resultView').hidden && !$('#confirm').open) { ev.preventDefault(); up(); }
  if (ev.key === 's' && !$('#resultView').hidden) $('#viewSeg button[data-view="sunburst"]').click();
  if (ev.key === 't' && !$('#resultView').hidden) $('#viewSeg button[data-view="treemap"]').click();
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', requestDraw);

// ---------- idle heartbeat ----------
// The server stops after a period of inactivity. Ping it regularly, but only report
// "active" if the user has actually done something, so a forgotten tab doesn't keep it alive.
let interacted = false;
for (const ev of ['pointermove', 'pointerdown', 'keydown', 'wheel']) {
  addEventListener(ev, () => { interacted = true; }, { passive: true, capture: true });
}
setInterval(() => {
  const active = interacted;
  interacted = false;
  api('/api/ping?active=' + (active ? 1 : 0)).catch(() => {});
}, 30000);

function showStopped() {
  $('#stopped').hidden = false;
}

init().catch((e) => {
  if (!$('#stopped').hidden) return;
  document.body.innerHTML = `<div class="empty" style="margin:auto">Couldn't reach the Diskscape server (${esc(e.message)}).<br>Open the URL printed in the terminal, including its <code>?t=</code> token.</div>`;
});

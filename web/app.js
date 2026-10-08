import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import * as satellite from './vendor/satellite.es.js';

const $ = (id) => document.getElementById(id);
const rad = (d) => d * Math.PI / 180;
const KM = 1 / 6371;
const SLOT_MS = 15000;
const VALUE = { HK: 10, IMG: 4, VOICE: 2, IDLE: 0 };
const COLOR = { HK: 0x8fd3ff, IMG: 0xc2a6ff, VOICE: 0x63e2b7 };
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

const api = async (path, body) => {
  const r = await fetch(path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : undefined);
  const data = await r.json();
  if (!r.ok) throw Object.assign(new Error('api'), { status: r.status, data });
  return data;
};

const sim = { mode: 'live', t: Date.now(), speed: 20, paused: false };
let cfg, satrec, passes = [], cur = null, lastSlot = -1, gsVec, gsGd;

// ------------------------------------------------------------------ globe

const canvas = $('globe');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
renderer.setClearColor(0x06080b);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);
const controls = new OrbitControls(camera, canvas);
Object.assign(controls, { enablePan: false, enableDamping: true, dampingFactor: 0.08, minDistance: 1.6, maxDistance: 7, rotateSpeed: 0.5 });

const ll2v = (lat, lng, r = 1) => new THREE.Vector3(r * Math.cos(rad(lat)) * Math.cos(rad(lng)), r * Math.sin(rad(lat)), -r * Math.cos(rad(lat)) * Math.sin(rad(lng)));
const ecf2v = (p) => new THREE.Vector3(p.x * KM, p.z * KM, -p.y * KM);
const sunUniform = { value: new THREE.Vector3(1, 0, 0) };
const sizeUniform = { value: 8 };

function sunDirection(date) {
  const n = date / 86400000 + 2440587.5 - 2451545.0;
  const L = rad((280.460 + 0.9856474 * n) % 360), g = rad((357.528 + 0.9856003 * n) % 360);
  const lam = L + rad(1.915) * Math.sin(g) + rad(0.020) * Math.sin(2 * g), eps = rad(23.439);
  const x = Math.cos(lam), y = Math.cos(eps) * Math.sin(lam), z = Math.sin(eps) * Math.sin(lam);
  const th = satellite.gstime(new Date(date)), c = Math.cos(th), s = Math.sin(th);
  return new THREE.Vector3(c * x + s * y, z, -(-s * x + c * y)).normalize();
}

function buildGlobe() {
  scene.add(new THREE.Mesh(new THREE.SphereGeometry(0.995, 96, 96), new THREE.ShaderMaterial({
    uniforms: { uSun: sunUniform },
    vertexShader: 'varying vec3 vN; varying float vRim; void main(){ vN = normalize(position); vec4 mv = modelViewMatrix*vec4(position,1.); vRim = 1. - max(dot(normalize(normalMatrix*normal), normalize(-mv.xyz)), 0.); gl_Position = projectionMatrix*mv; }',
    fragmentShader: `uniform vec3 uSun; varying vec3 vN; varying float vRim;
      void main(){ float day = smoothstep(-.15,.25,dot(vN,uSun));
        vec3 col = mix(vec3(.018,.026,.036), vec3(.042,.066,.094), day) + vec3(.25,.5,.75)*pow(vRim,3.5)*.4*(.3+.7*day);
        gl_FragColor = vec4(col,1.); }`,
  })));
  scene.add(new THREE.Mesh(new THREE.SphereGeometry(1.12, 64, 64), new THREE.ShaderMaterial({
    side: THREE.BackSide, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
    vertexShader: 'varying float vD; void main(){ vec4 mv = modelViewMatrix*vec4(position,1.); vD = dot(normalize(normalMatrix*normal), normalize(mv.xyz)); gl_Position = projectionMatrix*mv; }',
    fragmentShader: 'varying float vD; void main(){ float i = pow(clamp(vD/.45, 0., 1.), 2.6); gl_FragColor = vec4(.3,.58,.95,1.)*i*.5; }',
  })));

  const stars = [];
  for (let i = 0; i < 700; i++) stars.push(...new THREE.Vector3().randomDirection().multiplyScalar(20 + Math.random() * 10).toArray());
  const sg = new THREE.BufferGeometry();
  sg.setAttribute('position', new THREE.Float32BufferAttribute(stars, 3));
  scene.add(new THREE.Points(sg, new THREE.PointsMaterial({ color: 0x6f7f92, size: 1.2, sizeAttenuation: false })));

  const img = new Image();
  img.onload = () => {
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const px = ctx.getImageData(0, 0, c.width, c.height).data;
    const pos = [], step = 0.95;
    for (let lat = -86; lat <= 86; lat += step) {
      const count = Math.max(1, Math.round(360 * Math.cos(rad(lat)) / step));
      for (let k = 0; k < count; k++) {
        const lng = -180 + (k + 0.5) * 360 / count;
        const x = Math.floor((lng + 180) / 360 * c.width), y = Math.floor((90 - lat) / 180 * c.height);
        if (px[(y * c.width + x) * 4] < 110) pos.push(...ll2v(lat, lng, 1).toArray());
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    scene.add(new THREE.Points(g, new THREE.ShaderMaterial({
      uniforms: { uSun: sunUniform, uSize: sizeUniform }, transparent: true, depthWrite: false,
      vertexShader: 'uniform vec3 uSun; uniform float uSize; varying float vDay; void main(){ vDay = smoothstep(-.12,.18,dot(normalize(position),uSun)); vec4 mv = modelViewMatrix*vec4(position,1.); gl_Position = projectionMatrix*mv; gl_PointSize = uSize / -mv.z; }',
      fragmentShader: 'varying float vDay; void main(){ vec2 c = gl_PointCoord - .5; if(dot(c,c) > .25) discard; gl_FragColor = vec4(mix(vec3(.30,.47,.66), vec3(.80,.92,1.), vDay), mix(.85,1.,vDay)); }',
    })));
  };
  img.src = 'vendor/earth-water.png';
}

const line = (n, color, opacity = 1, loop = false) => {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(n * 3), 3));
  const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
  const l = loop ? new THREE.LineLoop(g, m) : new THREE.Line(g, m);
  l.frustumCulled = false;
  scene.add(l);
  return l;
};
const setPts = (l, pts) => { pts.forEach((p, i) => l.geometry.attributes.position.setXYZ(i, p.x, p.y, p.z)); l.geometry.setDrawRange(0, pts.length); l.geometry.attributes.position.needsUpdate = true; };

function circleOn(center, rho, r = 1.002, n = 96) {
  const c = center.clone().normalize();
  const u = new THREE.Vector3(0, 1, 0).cross(c);
  if (u.lengthSq() < 1e-6) u.set(1, 0, 0);
  u.normalize();
  const v = c.clone().cross(u);
  return Array.from({ length: n }, (_, i) => {
    const th = i / n * 2 * Math.PI;
    return c.clone().multiplyScalar(Math.cos(rho)).addScaledVector(u, Math.sin(rho) * Math.cos(th)).addScaledVector(v, Math.sin(rho) * Math.sin(th)).multiplyScalar(r);
  });
}

const ORBIT_N = 220;
const orbitLine = line(ORBIT_N, 0x8fd3ff, 0.55);
const footprint = line(96, 0x8fd3ff, 0.35, true);
const horizon = line(96, 0xd7dde4, 0.22, true);
const nadir = line(2, 0xd7dde4, 0.25);
const beam = line(2, 0x8fd3ff, 0.9);
beam.visible = false;
const satMesh = new THREE.Mesh(new THREE.OctahedronGeometry(0.014), new THREE.MeshBasicMaterial({ color: 0xf2f6fa }));
scene.add(satMesh);
const gsRing = new THREE.Mesh(new THREE.RingGeometry(0.012, 0.016, 40), new THREE.MeshBasicMaterial({ color: 0xf2f6fa, side: THREE.DoubleSide, transparent: true }));
const gsPulse = new THREE.Mesh(new THREE.RingGeometry(0.016, 0.0185, 40), new THREE.MeshBasicMaterial({ color: 0x8fd3ff, side: THREE.DoubleSide, transparent: true, depthWrite: false }));
const gsDot = new THREE.Mesh(new THREE.CircleGeometry(0.006, 20), new THREE.MeshBasicMaterial({ color: 0x8fd3ff, side: THREE.DoubleSide }));
scene.add(gsRing, gsPulse, gsDot);

const packets = Array.from({ length: 40 }, () => {
  const m = new THREE.Mesh(new THREE.SphereGeometry(0.0075, 10, 10), new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false }));
  m.visible = false;
  scene.add(m);
  return { m, born: 0, dur: 0, ok: true, live: false };
});
function spawnPackets(slot) {
  if (slot.action === 'IDLE') return;
  for (let k = 0; k < 3; k++) {
    const p = packets.find((q) => !q.live);
    if (!p) return;
    Object.assign(p, { live: true, born: performance.now() + k * 110, dur: 620, ok: slot.ok });
    p.m.material.color.setHex(COLOR[slot.action]);
  }
}

function propagate(t) {
  const d = new Date(t), pv = satellite.propagate(satrec, d);
  if (!pv.position) return null;
  const ecf = satellite.eciToEcf(pv.position, satellite.gstime(d));
  return { ecf, v: ecf2v(ecf), look: satellite.ecfToLookAngles(gsGd, ecf) };
}

let orbitAt = 0;
function updateOrbit(t) {
  if (Math.abs(t - orbitAt) < 20000) return;
  orbitAt = t;
  const pts = [];
  for (let i = 0; i < ORBIT_N; i++) { const p = propagate(t + (i - 20) * 30000); if (p) pts.push(p.v); }
  setPts(orbitLine, pts);
}

function resize() {
  const w = innerWidth, wide = w > 1180, h = wide ? innerHeight : innerHeight * 0.5;
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  if (wide) camera.setViewOffset(w, h, 0, 58, w, h); else camera.clearViewOffset();
  camera.updateProjectionMatrix();
  sizeUniform.value = 7.2 * renderer.getPixelRatio() * h / 820;
}
addEventListener('resize', resize);

let camTween = null;
function flyTo(lat, lng, dist) {
  const from = camera.position.clone(), to = ll2v(lat, lng, dist), t0 = performance.now();
  if (reduced) { camera.position.copy(to); return; }
  camTween = (now) => {
    const f = Math.min((now - t0) / 1400, 1), e = 1 - Math.pow(1 - f, 3);
    camera.position.copy(from.clone().lerp(to, e).setLength(from.length() + (dist - from.length()) * e));
    if (f >= 1) camTween = null;
  };
}

// ------------------------------------------------------------------ pass replay

const slotOf = (t) => (cur ? Math.floor((t - cur.aos) / SLOT_MS) : -1);
const fmtClock = (t) => new Date(t).toISOString().slice(11, 19);
const mmss = (s) => `${String(Math.floor(Math.abs(s) / 60)).padStart(2, '0')}:${String(Math.floor(Math.abs(s) % 60)).padStart(2, '0')}`;

function toast(text, info = false, ms = 5200) {
  const el = $('toast');
  el.textContent = text;
  el.classList.toggle('info', info);
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, ms);
}

function renderPasses() {
  $('passes').innerHTML = passes.map((p, i) => {
    const aos = new Date(p.aos), mins = Math.round((aos - Date.now()) / 60000);
    const when = aos.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false });
    const day = aos.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', weekday: 'short' });
    const dur = p.elev.length * 15, lit = p.sunlit.filter(Boolean).length / p.sunlit.length;
    return `<li data-i="${i}" tabindex="0"><span class="when">${day} ${when} IST</span><span class="max">${p.max_elev.toFixed(0)}°</span>
      <span class="sub">in ${mins >= 60 ? Math.floor(mins / 60) + ' h ' : ''}${mins % 60} min · ${Math.floor(dur / 60)} min ${dur % 60} s</span>
      <span class="sub">${lit > 0.9 ? 'sunlit' : lit < 0.1 ? 'eclipse' : 'part eclipse'}</span>
      <span class="elbar"><i style="width:${p.max_elev / 90 * 100}%"></i></span></li>`;
  }).join('') || '<li class="sub">No passes in the next 36 hours.</li>';
}

function drawSky() {
  const xy = (az, el) => { const r = (90 - el) / 90 * 100; return [r * Math.sin(rad(az)), -r * Math.cos(rad(az))]; };
  let s = [100, 66.7, 33.3].map((r) => `<circle class="ring" r="${r}"/>`).join('') +
    '<path class="ring" d="M-100 0H100M0 -100V100"/><text y="-106">N</text><text x="106">E</text><text y="106">S</text><text x="-106">W</text>';
  if (cur) {
    const pts = cur.p.az.map((a, i) => xy(a, cur.p.elev[i]));
    const k = Math.min(Math.max(lastSlot, -1), pts.length - 1);
    s += `<polyline class="arc" points="${pts.map((p) => p.join(',')).join(' ')}"/>`;
    if (k >= 0) s += `<polyline class="done" points="${pts.slice(0, k + 1).map((p) => p.join(',')).join(' ')}"/><circle class="dot" r="4" cx="${pts[k][0]}" cy="${pts[k][1]}"/>`;
  }
  $('sky').innerHTML = s;
  $('skyinfo').textContent = cur ? `peak ${cur.p.max_elev.toFixed(0)}° above horizon` : 'no pass selected';
}

function drawSpark() {
  const n = cur.n, y = (db) => 30 - Math.min(Math.max(db, 0), 24) / 24 * 30;
  const pts = cur.plan.ai.map((s, i) => `${(i + 0.5) / n * 100},${y(s.snr).toFixed(2)}`).join(' ');
  const cx = Math.min(Math.max(lastSlot + 0.5, 0), n) / n * 100;
  $('spark').innerHTML = `<line class="th hk" x1="0" x2="100" y1="${y(4)}" y2="${y(4)}"/><line class="th voice" x1="0" x2="100" y1="${y(9)}" y2="${y(9)}"/>
    <line class="th img" x1="0" x2="100" y1="${y(13)}" y2="${y(13)}"/><polyline class="snr" points="${pts}"/><line class="cursor" x1="${cx}" x2="${cx}" y1="0" y2="30"/>`;
}

function paintTapes() {
  for (const [id, key] of [['tape-ai', 'ai'], ['tape-base', 'baseline']]) {
    const el = $(id);
    if (el.children.length !== cur.n) el.innerHTML = '<i></i>'.repeat(cur.n);
    cur.plan[key].forEach((s, i) => {
      const played = i <= lastSlot;
      el.children[i].className = played ? `${s.action} played${s.ok || s.action === 'IDLE' ? '' : ' lost'}${s.source === 'watchdog' || s.source === 'operator' ? ' wd' : ''}${i === lastSlot ? ' now' : ''}` : '';
      el.children[i].title = played ? `T+${mmss(i * 15)} ${s.action}${s.action === 'IDLE' ? '' : s.ok ? ' delivered' : ' lost'} · ${s.snr} dB` : '';
    });
  }
}

function tally(key) {
  const out = { value: 0, HK: 0, IMG: 0, VOICE: 0, lost: 0 };
  for (let i = 0; i <= Math.min(lastSlot, cur.n - 1); i++) {
    const s = cur.plan[key][i];
    if (s.action === 'IDLE') continue;
    if (s.ok) { out.value += VALUE[s.action]; out[s.action]++; } else out.lost++;
  }
  return out;
}

function paintScores() {
  for (const [key, id] of [['ai', 'ai'], ['baseline', 'base']]) {
    const t = tally(key);
    $('s-' + id).textContent = t.value;
    const soc = lastSlot >= 0 ? cur.plan[key][Math.min(lastSlot, cur.n - 1)].soc.toFixed(0) + '%' : '–';
    $('d-' + id).innerHTML = `HK ${t.HK} · image ${t.IMG} · voice ${t.VOICE}<br>${t.lost} lost · battery ${soc}`;
  }
}

function setBatt(id, soc) {
  const el = $(id);
  el.style.width = soc + '%';
  el.className = soc <= 21 ? 'floor' : soc <= 40 ? 'low' : '';
  $(id + '-n').textContent = soc.toFixed(0) + '%';
}

function paintInstruments(k) {
  const a = cur.plan.ai[k], b = cur.plan.baseline[k];
  $('snr').textContent = a.snr.toFixed(1);
  $('snrfill').style.width = Math.min(Math.max(a.snr, 0), 24) / 24 * 100 + '%';
  $('elev').textContent = cur.p.elev[k].toFixed(0) + '°';
  $('range').textContent = Math.round(cur.p.range_km[k]) + ' km';
  setBatt('batt-ai', a.soc);
  setBatt('batt-base', b.soc);
  $('sun').textContent = a.sunlit ? 'sunlit · charging' : 'eclipse · no charging';
  ['hk', 'img', 'voice'].forEach((c, i) => { $('q-' + c).innerHTML = '<i></i>'.repeat(a.q[i]); });
}

function logSlot(k) {
  const s = cur.plan.ai[k], li = document.createElement('li');
  if (s.source !== 'model') li.className = s.source;
  const [why, tree] = s.reason.split('  [tree: ');
  li.innerHTML = `<span class="t">${mmss(k * 15)}</span><span class="a ${s.action}${s.ok || s.action === 'IDLE' ? '' : ' lost'}">${s.action === 'VOICE' ? 'VOICE' : s.action}</span>
    <span>${s.source === 'watchdog' ? 'Watchdog: ' : ''}${why}${s.action !== 'IDLE' && !s.ok ? ' — lost' : ''}${tree ? `<small>${tree.replace(']', '')}</small>` : ''}</span>`;
  const log = $('log');
  log.querySelector('.empty')?.remove();
  log.prepend(li);
  while (log.children.length > 60) log.lastChild.remove();
}

function reachSlot(k) {
  lastSlot = k;
  logSlot(k);
  spawnPackets(cur.plan.ai[k]);
  paintInstruments(k);
  paintTapes();
  paintScores();
  drawSky();
  drawSpark();
  beam.material.color.setHex(cur.plan.ai[k].source === 'model' ? 0x8fd3ff : 0xffb020);
}

async function plan() {
  cur.plan = await api('/api/plan', {
    pass: { elev: cur.p.elev, range_km: cur.p.range_km, sunlit: cur.p.sunlit },
    start: { soc: +$('c-soc').value, hk: 5, img: 14, voice: 8 }, seed: cur.seed, events: cur.events,
  });
  paintTapes();
  drawSpark();
  if (lastSlot < 0) { setBatt('batt-ai', +$('c-soc').value); setBatt('batt-base', +$('c-soc').value); }
}

async function playPass(i) {
  const p = passes[i], n = p.elev.length;
  cur = { p, n, i, aos: Date.parse(p.aos), seed: 1000 + i,
    events: { extra_loss_db: Array(n).fill(+$('c-loss').value), eclipse: Array(n).fill(false), safe_mode: Array(n).fill(false),
      corrupt: Array(n).fill(false), model_fault: Array(n).fill(false), soc_set: Array(n).fill(null) } };
  lastSlot = -1;
  $('tape-ai').innerHTML = $('tape-base').innerHTML = '';
  $('log').innerHTML = '<li class="empty">Waiting for the satellite to rise above the horizon.</li>';
  ['f-eclipse', 'f-safe'].forEach((id) => $(id).setAttribute('aria-pressed', 'false'));
  $('c-soc').disabled = false;
  await plan();
  Object.assign(sim, { mode: 'replay', t: cur.aos - 6 * SLOT_MS, paused: false });
  $('pause').textContent = 'Pause';
  $('jump').textContent = 'Back to live';
  document.querySelectorAll('#passes li').forEach((li) => li.classList.toggle('on', +li.dataset.i === i));
  ['hk', 'img', 'voice'].forEach((c, k) => { $('q-' + c).innerHTML = '<i></i>'.repeat([5, 14, 8][k]); });
  paintScores();
  drawSky();
  flyTo(cfg.ground.lat - 4, cfg.ground.lng, 3.1);
}

function goLive() {
  cur = null; lastSlot = -1;
  Object.assign(sim, { mode: 'live', t: Date.now(), paused: false });
  $('jump').textContent = 'Play next pass';
  $('c-soc').disabled = false;
  beam.visible = false;
  document.querySelectorAll('#passes li').forEach((li) => li.classList.remove('on'));
  drawSky();
}

// Change the schedule from the next slot onward; the past of the pass never changes.
async function change(fn, needsPass = true) {
  if (!cur || lastSlot >= cur.n - 1) { if (needsPass) toast('Play a pass first, then use the operator controls during it.', true); return false; }
  fn(Math.max(lastSlot + 1, 0), cur.events, cur.n);
  await plan();
  return true;
}
const fillFrom = (arr, k, v, len = Infinity) => { for (let t = k; t < Math.min(arr.length, k + len); t++) arr[t] = v; };

function wireControls() {
  $('jump').onclick = () => {
    if (sim.mode === 'replay') return goLive();
    const i = passes.findIndex((p) => p.max_elev >= 25 && Date.parse(p.aos) > Date.now());
    playPass(i >= 0 ? i : 0);
  };
  $('passes').onclick = (e) => { const li = e.target.closest('li[data-i]'); if (li) playPass(+li.dataset.i); };
  $('passes').onkeydown = (e) => { if (e.key === 'Enter') e.target.closest('li[data-i]')?.click(); };
  $('pause').onclick = () => { sim.paused = !sim.paused; $('pause').textContent = sim.paused ? 'Resume' : 'Pause'; };
  document.querySelectorAll('[data-speed]').forEach((b) => { b.onclick = () => {
    sim.speed = +b.dataset.speed;
    document.querySelectorAll('[data-speed]').forEach((x) => x.classList.toggle('on', x === b));
  }; });
  $('c-soc').oninput = () => { $('o-soc').textContent = $('c-soc').value + '%'; };
  $('c-soc').onchange = () => { if (cur && lastSlot < 0) plan(); };
  $('c-loss').oninput = () => { $('o-loss').textContent = $('c-loss').value + ' dB'; };
  $('c-loss').onchange = () => change((k, ev) => fillFrom(ev.extra_loss_db, k, +$('c-loss').value), false);
  $('f-eclipse').onclick = async () => {
    const on = $('f-eclipse').getAttribute('aria-pressed') !== 'true';
    if (await change((k, ev) => fillFrom(ev.eclipse, k, on))) $('f-eclipse').setAttribute('aria-pressed', on);
  };
  $('f-safe').onclick = async () => {
    const on = $('f-safe').getAttribute('aria-pressed') !== 'true';
    if (await change((k, ev) => fillFrom(ev.safe_mode, k, on))) $('f-safe').setAttribute('aria-pressed', on);
  };
  $('f-drain').onclick = () => change((k, ev) => { ev.soc_set[k] = 28; });
  $('f-fault').onclick = async () => {
    if (await change((k, ev) => fillFrom(ev.model_fault, k, true, 5))) toast('Model output corrupted for 5 slots. The watchdog rejects it and uses the fallback rule.');
  };
  $('f-corrupt').onclick = async () => {
    if (!(await change((k, ev) => fillFrom(ev.corrupt, k, true, 3)))) return;
    try {   // the same bad frame, sent to the router API, is refused with a validation error
      await api('/api/decide', { snr_db: 11, elev_deg: 30, rising: true, max_elev_deg: 60, slots_left: 12, soc_pct: -40, sunlit: true, hk_queued: 2, img_queued: 4, voice_queued: 1 });
    } catch (e) {
      const d = e.data?.detail?.[0];
      toast(`Router API rejected the sensor frame (HTTP ${e.status}): ${d ? d.loc.at(-1) + ' — ' + d.msg : 'invalid input'}. Watchdog sends housekeeping only for 3 slots.`, false, 7000);
    }
  };
  document.querySelectorAll('.tabs button').forEach((b) => { b.onclick = () => {
    document.querySelectorAll('.tabs button').forEach((x) => x.classList.toggle('on', x === b));
    $('view-real').hidden = b.dataset.view !== 'real';
    $('view-model').hidden = b.dataset.view !== 'model';
  }; });
}

// ------------------------------------------------------------------ drawers

function renderReal(r) {
  const total = Object.values(r.frame_mix).reduce((a, b) => a + b, 0), pay = r.frame_mix.payload || 0;
  const rows = r.uhf_beacons.slice(-14).reverse().map((b) => `<tr><td>${b.station}</td><td>${(b.t || '').slice(11, 19)}</td><td>${b.trx_temp_c.toFixed(1)}</td><td>${b.pa_temp_c.toFixed(1)}</td><td>${b.rssi_raw}</td><td>${(b.uptime_s / 86400).toFixed(1)}</td></tr>`).join('');
  $('view-real').innerHTML = `<h3>Real telemetry from orbit</h3>
    <p>${cfg.stand_in} Everything on this page was received by volunteer ground stations and decoded here; nothing is simulated.</p>
    <div class="stats"><div><b>${r.passes}</b><span>real passes, ${r.window[0].slice(11, 16)}–${r.window[1].slice(11, 16)} UTC on ${r.window[0].slice(0, 10)}</span></div>
      <div><b>${r.stations.length}</b><span>ground stations that heard it (dots on the globe)</span></div>
      <div><b>${total}</b><span>frames downloaded and classified</span></div>
      <div><b>${(pay / total * 100).toFixed(1)}%</b><span>of frames were image/file data</span></div></div>
    <h4>What the satellite was sending</h4>
    <div class="mix"><i style="width:${pay / total * 100}%"></i><i style="width:${(1 - pay / total) * 100}%"></i></div>
    <p>${pay} image/file chunks against ${total - pay} housekeeping beacons and messages. This is the contention the SomaiyaSat router has to manage: bulk payload competing with a trickle of critical housekeeping.</p>
    <h4>Housekeeping beacons (radio board)</h4>
    <table><tr><th>Heard by</th><th>UTC</th><th>Radio °C</th><th>Amplifier °C</th><th>RSSI raw</th><th>Uptime days</th></tr>${rows}</table>
    <p class="note" style="margin-top:6px">${r.temp_scale_note}. No battery beacons were downlinked in this window, so battery behaviour in the pass view is modelled.</p>
    <h4>Messages carried</h4>${r.messages.slice(0, 3).map((m) => `<blockquote>${m.text}</blockquote>`).join('')}
    <p class="note">Source: ${r.source}.</p>`;
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(r.stations.flatMap((s) => ll2v(s.lat, s.lng, 1.004).toArray()), 3));
  scene.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0xffffff, size: 3.2 * Math.min(devicePixelRatio, 2), sizeAttenuation: false, transparent: true, opacity: 0.85 })));
}

function renderModel(m) {
  const names = { fixed_priority_baseline: 'Fixed priority (baseline)', rule_fallback_only: 'Fallback rule alone', ai_tree_with_watchdog: 'AI tree + watchdog', oracle_upper_bound: 'Oracle (upper bound)' };
  const rows = Object.entries(m.policies).map(([k, v]) => `<tr class="${k.startsWith('ai') ? 'hl' : ''}"><td>${names[k]}</td><td>${v.mean_delivered_value}</td><td>${v.hk_delivered_pct_of_generated}%</td><td>${v.img_chunks_delivered_per_pass}</td><td>${v.voice_delivered_per_pass}</td><td>${v.transmissions_lost_pct}%</td><td>${v.mean_soc_end_pct}%</td></tr>`).join('');
  const imp = Object.entries(m.model.feature_importance).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<div class="imp"><span>${k.replace(/_/g, ' ')}</span><i style="width:${v * 100 / 0.5}%"></i><output>${(v * 100).toFixed(0)}%</output></div>`).join('');
  const cm = m.model.confusion_matrix_rows_true_cols_pred;
  $('view-model').innerHTML = `<h3>The router model</h3>
    <p>A decision tree trained to imitate a dynamic-programming solver that knows the link in advance. It runs as plain comparisons, with no ML library on board.</p>
    <div class="stats"><div><b>${(m.model.accuracy_vs_oracle_test * 100).toFixed(1)}%</b><span>agreement with the solver on unseen passes</span></div>
      <div><b>${m.model.nodes}</b><span>tree nodes, depth ${m.model.depth}</span></div>
      <div><b>${(m.model.file_bytes / 1024).toFixed(1)} kB</b><span>model file</span></div>
      <div><b>${m.model.inference_microseconds} µs</b><span>per decision on this machine</span></div></div>
    <h4>Average per pass over ${m.evaluation_passes} unseen passes</h4>
    <table><tr><th>Policy</th><th>Value delivered</th><th>Housekeeping delivered</th><th>Image chunks</th><th>Voice</th><th>Transmissions lost</th><th>Battery at end</th></tr>${rows}</table>
    <p class="note" style="margin-top:6px">${m.note}</p>
    <h4>What the tree looks at</h4>${imp}
    <h4>Tree choice against solver choice (unseen decisions)</h4>
    <table><tr><th>Solver ↓ / tree →</th>${cm.labels.map((l) => `<th>${l}</th>`).join('')}</tr>${cm.matrix.map((r, i) => `<tr><td>${cm.labels[i]}</td>${r.map((v) => `<td>${v}</td>`).join('')}</tr>`).join('')}</table>`;
}

// ------------------------------------------------------------------ frame loop

let prev = performance.now();
function frame(now) {
  const dt = Math.min(now - prev, 100);
  prev = now;
  if (sim.mode === 'live') sim.t = Date.now();
  else if (!sim.paused) sim.t += dt * sim.speed;

  const here = propagate(sim.t);
  if (here) {
    sunUniform.value.copy(sunDirection(sim.t));
    satMesh.position.copy(here.v);
    satMesh.rotation.y += dt * 0.0006;
    const sub = here.v.clone().normalize(), altKm = (here.v.length() - 1) / KM;
    setPts(nadir, [here.v, sub.clone().multiplyScalar(1.002)]);
    setPts(footprint, circleOn(sub, Math.acos(1 / here.v.length())));
    updateOrbit(sim.t);

    const el = here.look.elevation * 180 / Math.PI, inPass = cur && sim.t >= cur.aos && lastSlot < cur.n;
    beam.visible = !!inPass && el > 0;
    if (beam.visible) {
      setPts(beam, [here.v, gsVec]);
      const s = cur.plan.ai[Math.min(Math.max(lastSlot, 0), cur.n - 1)];
      beam.material.opacity = 0.15 + 0.75 * Math.min(Math.max(s.snr, 0), 20) / 20;
    }
    for (const p of packets) {
      if (!p.live) continue;
      const f = (now - p.born) / p.dur;
      if (f < 0) continue;
      if (f >= 1 || !beam.visible) { p.live = false; p.m.visible = false; if (p.ok) gsPulse.userData.hit = now; continue; }
      p.m.visible = true;
      p.m.position.lerpVectors(here.v, gsVec, f);
      p.m.material.opacity = p.ok ? 1 : Math.max(0, 1 - f / 0.6);
      p.m.scale.setScalar(p.ok ? 1 : 1 + f * 1.4);
    }
    const hit = Math.min((now - (gsPulse.userData.hit || -1e9)) / 500, 1);
    const idle = reduced ? 0 : (now % 2400) / 2400;
    gsPulse.scale.setScalar(hit < 1 ? 1 + hit * 1.6 : 1 + idle * 2.2);
    gsPulse.material.opacity = hit < 1 ? 1 - hit : 0.55 * (1 - idle);

    const sp = here.v.clone().project(camera), toSat = here.v.clone().sub(camera.position);
    const tc = -camera.position.dot(toSat) / toSat.lengthSq();
    const hidden = tc > 0 && tc < 1 && camera.position.clone().addScaledVector(toSat, tc).length() < 0.995;
    const label = $('satlabel');
    label.hidden = hidden || sp.z > 1;
    label.style.left = (sp.x + 1) / 2 * innerWidth + 'px';
    label.style.top = (1 - sp.y) / 2 * canvas.clientHeight + 'px';
    $('satlabel-sub').textContent = `${altKm.toFixed(0)} km · ${el > 0 ? 'elev ' + el.toFixed(0) + '°' : 'below horizon'}`;

    if (!inPass) {
      $('snr').textContent = '–';
      $('snrfill').style.width = '0';
      $('elev').textContent = el > 0 ? el.toFixed(0) + '°' : 'below horizon';
      $('range').textContent = Math.round(here.look.rangeSat) + ' km';
    }
  }

  $('utc').textContent = fmtClock(sim.t);
  $('mode').textContent = sim.mode === 'live' ? 'Live' : sim.paused ? 'Paused' : `Replay ${sim.speed}×`;
  $('mode').classList.toggle('replay', sim.mode !== 'live');
  if (cur) {
    const s = (sim.t - cur.aos) / 1000;
    $('tplus').textContent = s < 0 ? `rises in ${mmss(s)}` : `T+${mmss(Math.min(s, cur.n * 15))}`;
    const k = Math.min(slotOf(sim.t), cur.n - 1);
    if (k >= 0) $('c-soc').disabled = true;
    for (let j = lastSlot + 1; j <= k; j++) reachSlot(j);
    if (slotOf(sim.t) >= cur.n && !cur.done) {
      cur.done = true;
      sim.paused = true;
      const a = cur.plan.totals.ai, b = cur.plan.totals.baseline;
      const lost = (x) => x.lost.reduce((p, q) => p + q, 0);
      toast(`Pass complete. AI router: ${a.value} points delivered, ${lost(a)} transmissions lost, battery ${a.soc_end.toFixed(0)}%. Fixed priority: ${b.value} points, ${lost(b)} lost, battery ${b.soc_end.toFixed(0)}%${b.soc_end <= 21 ? ' (drained to the floor)' : ''}.`, true, 14000);
    }
  } else {
    const next = passes.find((p) => Date.parse(p.aos) > sim.t);
    $('tplus').textContent = next ? `next pass in ${Math.round((Date.parse(next.aos) - sim.t) / 60000)} min` : '';
  }

  camTween?.(now);
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(frame);
}

async function init() {
  wireControls();
  buildGlobe();
  resize();
  cfg = await api('/api/config');
  satrec = satellite.twoline2satrec(cfg.tle.tle1, cfg.tle.tle2);
  gsGd = { latitude: rad(cfg.ground.lat), longitude: rad(cfg.ground.lng), height: cfg.ground.alt_km };
  gsVec = ll2v(cfg.ground.lat, cfg.ground.lng, 1.003);
  for (const m of [gsRing, gsPulse, gsDot]) { m.position.copy(gsVec); m.lookAt(gsVec.clone().multiplyScalar(2)); }
  const altNow = propagate(Date.now());
  setPts(horizon, circleOn(gsVec, Math.acos(1 / (altNow ? altNow.v.length() : 1.08))));
  camera.position.copy(ll2v(12, cfg.ground.lng + 6, 3.4));
  passes = (await api('/api/passes')).passes;
  renderPasses();
  drawSky();
  requestAnimationFrame(frame);
  api('/api/real').then(renderReal);
  api('/api/metrics').then(renderModel);
  window.__gs = { sim, playPass, get cur() { return cur; }, get lastSlot() { return lastSlot; } };
}
init().catch((e) => { console.error(e); toast('Could not reach the ground-station service. Start it with: uvicorn app.main:app'); });

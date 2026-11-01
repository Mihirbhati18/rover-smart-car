const $ = id => document.getElementById(id);
const NS = 'http://www.w3.org/2000/svg';
let state = {}, lastCommand = '', commandTimer = null, micOn = false, recognition = null, quickHoldSent = false;
let speaking = false, lastSpeechId = null;

async function api(action, data = {}) {
  try {
    const response = await fetch('/api/' + action, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Session': window.SESSION_TOKEN}, body: JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    if (action !== 'heartbeat') { $('error').hidden = true; $('startError').hidden = true; }
    return result;
  } catch (error) {
    if (action === 'heartbeat') return null;
    const target = $('start').hidden ? $('error') : $('startError');
    target.textContent = error.message; target.hidden = false;
    clearTimeout(api.timer); api.timer = setTimeout(() => { $('error').hidden = true; }, 6000);
    return null;
  }
}
const send = text => { if (text && text.trim()) api('vr_command', {text}); };

// ---------- compass ----------
(function buildCompass() {
  const strip = $('compass');
  for (let h = -360; h <= 360; h += 15) {
    const span = document.createElement('span');
    const a = ((h % 360) + 540) % 360 - 180;
    span.textContent = a === 0 ? 'FWD' : Math.abs(a) === 180 ? 'BACK' : a === 90 ? 'R90' : a === -90 ? 'L90' : (a > 0 ? 'R' : 'L') + Math.abs(a);
    if (a % 45 === 0) span.className = 'major';
    strip.append(span);
  }
})();
function setCompass(heading) {
  const box = $('compass').parentElement.clientWidth;
  const index = (heading + 360) / 15;
  $('compass').style.transform = `translateX(${box / 2 - (index * 40 + 20)}px)`;
}

// ---------- speed gauge ----------
const R = 85, polar = (deg, r) => [r * Math.sin(deg * Math.PI / 180), -r * Math.cos(deg * Math.PI / 180)];
(function buildGauge() {
  const svg = $('gauge');
  for (let i = 1; i <= 10; i++) {
    const a0 = -120 + (i - 1) * 24 + 1.5, a1 = -120 + i * 24 - 1.5;
    const [x0, y0] = polar(a0, R), [x1, y1] = polar(a1, R);
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', `M${x0.toFixed(1)} ${y0.toFixed(1)} A${R} ${R} 0 0 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`);
    path.setAttribute('class', 'gauge-seg'); path.dataset.level = i;
    svg.append(path);
  }
  for (const i of [1, 5, 10]) {
    const [x, y] = polar(-120 + i * 24 - 12, 104);
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', x.toFixed(1)); t.setAttribute('y', (y + 4).toFixed(1)); t.setAttribute('class', 'gauge-tick'); t.textContent = i;
    svg.append(t);
  }
  const needle = document.createElementNS(NS, 'line');
  // Short pointer just inside the ring, so it never crosses the level number.
  needle.setAttribute('x1', 0); needle.setAttribute('y1', -58); needle.setAttribute('x2', 0); needle.setAttribute('y2', -76);
  needle.setAttribute('class', 'gauge-needle'); needle.id = 'needle';
  needle.style.transformOrigin = '0px 0px';
  svg.append(needle);
})();
function setGauge(level, speed) {
  document.querySelectorAll('.gauge-seg').forEach(seg => {
    const n = Number(seg.dataset.level);
    seg.classList.toggle('lit', n <= level);
    seg.classList.toggle('hot', n >= 7 && n <= 9);
    seg.classList.toggle('max', n === 10);
  });
  $('needle').style.transform = `rotate(${-120 + level * 24 - 12}deg)`;
  $('level').textContent = level;
  $('speedEst').textContent = `≈ ${speed} cm/s est.`;
}

// ---------- minimap ----------
let lastMap = '';
function drawMap(points, heading, auto, route) {
  const key = JSON.stringify([points, heading, auto && [auto.line, auto.obstacles], route]); if (key === lastMap) return; lastMap = key;
  const svg = $('map'); svg.replaceChildren();
  const extra = (auto ? auto.line.concat(auto.obstacles) : []).concat(route || []);
  const xs = points.concat(extra).map(p => p[0]).concat(0), ys = points.concat(extra).map(p => -p[1]).concat(0);
  let minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const span = Math.max(100, maxX - minX + 40, maxY - minY + 40);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  svg.setAttribute('viewBox', `${cx - span / 2} ${cy - span / 2} ${span} ${span}`);
  const el = (name, attrs) => { const e = document.createElementNS(NS, name); for (const k in attrs) e.setAttribute(k, attrs[k]); e.setAttribute('vector-effect', 'non-scaling-stroke'); svg.append(e); return e; };
  const step = span > 300 ? 50 : 25;
  for (let g = Math.floor((cx - span / 2) / step) * step; g <= cx + span / 2; g += step) el('line', {x1: g, y1: cy - span / 2, x2: g, y2: cy + span / 2, class: 'map-grid'});
  for (let g = Math.floor((cy - span / 2) / step) * step; g <= cy + span / 2; g += step) el('line', {x1: cx - span / 2, y1: g, x2: cx + span / 2, y2: g, class: 'map-grid'});
  el('circle', {cx: 0, cy: 0, r: span / 60, class: 'map-start'});
  if (route && route.length) {
    const last = points[points.length - 1];
    el('polyline', {points: [last].concat(route).map(p => `${p[0]},${-p[1]}`).join(' '), class: 'map-route'});
  }
  if (auto) {
    const [a, b] = auto.line;
    el('line', {x1: a[0], y1: -a[1], x2: b[0], y2: -b[1], class: 'map-plan'});
    el('circle', {cx: b[0], cy: -b[1], r: span / 35, class: 'map-goal'});
    for (const o of auto.obstacles) el('circle', {cx: o[0], cy: -o[1], r: span / 50, class: 'map-obst'});
  }
  el('polyline', {points: points.map(p => `${p[0]},${-p[1]}`).join(' '), class: 'map-path'});
  const last = points[points.length - 1], s = span / 22;
  el('polygon', {points: `0,${-s} ${s * .7},${s * .7} 0,${s * .35} ${-s * .7},${s * .7}`, class: 'map-car', transform: `translate(${last[0]},${-last[1]}) rotate(${heading})`});
  let travelled = 0; for (let i = 1; i < points.length; i++) travelled += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
  $('mapInfo').textContent = `${Math.round(travelled)} cm`;
}

// ---------- render ----------
function glow(id, value) {
  const el = $(id); el.style.opacity = value ? Math.min(1, 0.25 + value * 0.75) : 0;
  el.classList.toggle('critical', value >= 0.8);
}
function render(s) {
  state = s; const v = s.vr || {};
  $('start').hidden = !!v.active;
  $('doing').textContent = v.active ? (v.doing || 'IDLE') : 'OFFLINE';
  $('engineChip').textContent = v.engine ? 'ENGINE ON' : 'ENGINE OFF'; $('engineChip').classList.toggle('on', !!v.engine);
  $('engine').textContent = v.engine ? 'ENGINE ON' : 'START ENGINE'; $('engine').classList.toggle('on', !!v.engine);
  $('engine').disabled = !v.active || !!v.engine;
  $('linkChip').textContent = s.serial_connected ? (s.transport === 'ble' ? 'CAR · BLUETOOTH' : 'CAR · USB') : 'CAR OFFLINE';
  $('linkChip').classList.toggle('bad', !s.serial_connected);
  $('camChip').textContent = s.camera_connected ? 'CAM LIVE' : 'CAM OFFLINE'; $('camChip').classList.toggle('bad', !s.camera_connected);
  $('sonarChip').textContent = 'FRONT ' + (v.sonar == null ? '—' : v.sonar) + ' cm';
  const d = v.danger || {}; glow('glowFront', d.front || 0); glow('glowLeft', d.left || 0); glow('glowRight', d.right || 0);
  $('override').classList.toggle('on', !!v.override);
  setCompass(v.heading || 0);
  setGauge(v.level || 5, v.speed || 0);
  const ms = v.mission || {};
  drawMap(v.points || [[0, 0]], v.heading || 0, v.auto, ms.kind ? (ms.waypoints || []).slice(Math.max(0, (ms.index || 0) - 1)) : null);
  $('recChip').hidden = !ms.recording;
  $('missionChip').hidden = !ms.kind;
  if (ms.kind) $('missionChip').textContent = {route: 'REPEATING ROUTE', home: 'GOING HOME', goto: `GO TO ${(ms.label || '').toUpperCase()}`, follow: 'FOLLOW ME'}[ms.kind];
  $('missionOff').hidden = !ms.kind;
  drawDetections(ms.detections || [], ms.label);
  if (ms.speech && ms.speech.id !== lastSpeechId) { const first = lastSpeechId === null; lastSpeechId = ms.speech.id; if (!first) speak(ms.speech.text); }
  const a = v.auto, running = !!(a && !a.finished);
  $('autoChip').hidden = !a;
  if (a) {
    $('autoChip').textContent = a.finished ? 'AUTOPILOT DONE' : a.help ? 'AUTOPILOT · HELP' : a.paused ? 'AUTOPILOT PAUSED' : `AUTOPILOT ${Math.max(0, a.along)}/${a.target} cm`;
    $('autoChip').classList.toggle('paused', a.paused && !a.help); $('autoChip').classList.toggle('help', !!a.help);
  }
  $('autoBanner').hidden = !a || !a.status;
  if (a) {
    $('autoBanner').textContent = `AUTOPILOT · ${a.status} · ${Math.max(0, a.along)}/${a.target} cm, ${Math.abs(a.right)} cm ${a.right >= 0 ? 'right' : 'left'} of line`;
    $('autoBanner').classList.toggle('paused', a.paused && !a.help); $('autoBanner').classList.toggle('help', !!a.help);
  }
  if (ms.kind) {  // a mission's own status is more useful than its autopilot leg's
    $('autoBanner').hidden = false;
    $('autoBanner').textContent = `${$('missionChip').textContent} · ${ms.status}`;
    $('autoBanner').classList.toggle('paused', !!(a && a.paused && !a.help)); $('autoBanner').classList.toggle('help', !!(a && a.help));
  }
  $('resume').hidden = !(running && a.paused); $('autoOff').hidden = !running || !!ms.kind;
  if (v.last_command && v.last_command !== lastCommand) {
    lastCommand = v.last_command; $('command').textContent = v.last_command; $('command').classList.add('show');
    clearTimeout(commandTimer); commandTimer = setTimeout(() => $('command').classList.remove('show'), 3500);
  }
  $('message').hidden = !v.message; $('message').textContent = v.message || '';
  if (s.error && v.active === false && !$('start').hidden) { $('startError').textContent = s.error; $('startError').hidden = false; }
  const r = v.report;
  if (r && r.time !== render.reportTime) {
    render.reportTime = r.time; $('report').hidden = false; $('reportTime').textContent = r.time;
    $('reportLines').replaceChildren(...r.lines.map(line => { const li = document.createElement('li'); li.textContent = line; if (/obstacle/i.test(line)) li.className = 'warn'; return li; }));
    $('reportAdvice').textContent = 'RECOMMENDED: ' + r.advice; $('reportAdvice').classList.toggle('warn', /^(Stop|Slow)/.test(r.advice));
  }
}
// Detection boxes drawn over the camera picture (object-fit: contain, so match the shown image area).
let lastDet = '', frameW = 0, frameH = 0;  // last loaded frame size (reads 0 while the next frame loads)
function drawDetections(dets, target) {
  const img = $('feed'), svg = $('detBoxes');
  const key = JSON.stringify([dets, target, img.clientWidth, img.clientHeight, frameW]); if (key === lastDet) return; lastDet = key;
  svg.replaceChildren();
  if (!dets.length || !frameW) return;
  const scale = Math.min(img.clientWidth / frameW, img.clientHeight / frameH);
  const w = frameW * scale, h = frameH * scale;
  Object.assign(svg.style, {left: `${img.offsetLeft + (img.clientWidth - w) / 2}px`, top: `${img.offsetTop + (img.clientHeight - h) / 2}px`, width: `${w}px`, height: `${h}px`});
  for (const d of dets) {
    const [x0, y0, x1, y1] = d.box, isTarget = d.label === target;
    const add = (name, attrs, text) => { const e = document.createElementNS(NS, name); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; svg.append(e); };
    add('rect', {x: x0 * w, y: y0 * h, width: (x1 - x0) * w, height: (y1 - y0) * h, rx: 6, class: 'det-box' + (isTarget ? ' target' : '')});
    const label = `${d.label} ${Math.round(d.conf * 100)}%`;
    add('rect', {x: x0 * w, y: Math.max(0, y0 * h - 20), width: label.length * 7.4 + 10, height: 19, rx: 4, class: 'det-tag' + (isTarget ? ' target' : '')});
    add('text', {x: x0 * w + 5, y: Math.max(14, y0 * h - 6), class: 'det-label'}, label);
  }
}

// The car answers out loud. The microphone pauses while it speaks so it doesn't hear itself.
function speak(text) {
  if (!window.speechSynthesis) return;
  const utter = new SpeechSynthesisUtterance(text);
  utter.lang = 'en-IN'; utter.rate = 1.05;
  utter.onstart = () => { speaking = true; if (recognition) { recognition.onend = null; recognition.abort(); recognition = null; } };
  utter.onend = utter.onerror = () => { speaking = false; if (micOn && !recognition) startRecognition(); };
  speechSynthesis.cancel(); speechSynthesis.speak(utter);
  $('heard').textContent = '🔊 ' + text;
}

let polling = false;
async function poll() {
  if (polling) return; polling = true;
  try { const r = await fetch('/api/state'); if (r.ok) render(await r.json()); }
  catch (e) { $('doing').textContent = 'SERVER OFFLINE'; }
  finally { polling = false; }
}

// ---------- camera feed ----------
function nextFrame(delay) { setTimeout(() => { $('feed').src = '/frame.jpg?plain=1&t=' + Date.now(); }, delay); }
$('feed').onload = () => { $('noFeed').hidden = true; frameW = $('feed').naturalWidth; frameH = $('feed').naturalHeight; nextFrame(120); };
$('feed').onerror = () => { $('noFeed').hidden = false; nextFrame(1000); };
nextFrame(0);

// ---------- voice ----------
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
function setMic(on) {
  micOn = on && !!SpeechRecognition;
  $('mic').classList.toggle('listening', micOn); $('mic').setAttribute('aria-pressed', String(micOn));
  $('micLabel').textContent = micOn ? 'LISTENING' : (SpeechRecognition ? 'MIC OFF' : 'NO MIC (use Edge)');
  if (micOn && !recognition) startRecognition();
  if (!micOn && recognition) { recognition.onend = null; recognition.abort(); recognition = null; }
}
function startRecognition() {
  recognition = new SpeechRecognition();
  recognition.lang = 'en-IN'; recognition.continuous = true; recognition.interimResults = true;
  recognition.onresult = e => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = e.results[i][0].transcript.trim();
      $('heard').textContent = '🎙 ' + text;
      // Act on "stop"/"hold" as soon as it is heard, before the sentence is final.
      if (!quickHoldSent && /\b(stop|hold|halt|brake)\b/i.test(text)) { quickHoldSent = true; api('vr_hold'); }
      if (e.results[i].isFinal) {
        quickHoldSent = false;
        if (/\bemergency\b/i.test(text)) api('stop'); else send(text);
      }
    }
  };
  recognition.onerror = e => { if (e.error === 'not-allowed' || e.error === 'audio-capture') { setMic(false); $('error').textContent = 'Microphone: ' + e.error + '. Use Edge and allow the microphone.'; $('error').hidden = false; } };
  recognition.onend = () => { recognition = null; if (micOn) setTimeout(() => { if (micOn && !recognition && !speaking) startRecognition(); }, 250); };
  try { recognition.start(); } catch (e) { recognition = null; }
}
$('mic').onclick = () => setMic(!micOn);
if (!SpeechRecognition) setMic(false);

// ---------- buttons & keys ----------
$('enter').onclick = async () => {
  try { await document.documentElement.requestFullscreen({navigationUI: 'hide'}); } catch (e) {}
  const ok = await api('vr');
  if (ok && SpeechRecognition) setMic(true);
};
$('engine').onclick = () => api('vr_engine');
$('resume').onclick = () => send('resume');
$('autoOff').onclick = () => send('cancel autopilot');
$('missionOff').onclick = () => send('stop mission');
$('hold').onclick = () => api('vr_hold');
$('estop').onclick = () => api('stop');
$('reportClose').onclick = () => { $('report').hidden = true; };
$('resetPath').onclick = () => api('vr_reset_path');
$('cmdForm').onsubmit = e => { e.preventDefault(); const t = $('cmdText').value; $('cmdText').value = ''; if (/\bemergency\b/i.test(t)) api('stop'); else send(t); };
$('exit').onclick = async () => {
  setMic(false); await api('vr_exit');
  if (document.fullscreenElement) { try { await document.exitFullscreen(); } catch (e) {} }
  location.href = '/';
};
document.addEventListener('keydown', e => {
  if (e.target === $('cmdText') || e.repeat) return;
  const keys = {w: 'forward', arrowup: 'forward', s: 'reverse', arrowdown: 'reverse', a: 'left 15 degrees', arrowleft: 'left 15 degrees',
                d: 'right 15 degrees', arrowright: 'right 15 degrees', '+': 'faster', '=': 'faster', '-': 'slower', r: 'analyze'};
  const key = e.key.toLowerCase();
  if (key === ' ') { e.preventDefault(); api('vr_hold'); }
  else if (key === 'escape') { api('stop'); }
  else if (key === 'm') { setMic(!micOn); }
  else if (keys[key] && $('start').hidden) { e.preventDefault(); send(keys[key]); }
});

// ---------- safety: heartbeat, hidden tab, leaving ----------
setInterval(() => { if (!document.hidden) api('heartbeat', {vr: true}); }, 900);
document.addEventListener('visibilitychange', () => { if (document.hidden && state.vr && state.vr.active) { api('vr_hold'); api('disarm'); } });
window.addEventListener('pagehide', () => fetch('/api/vr_exit', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Session': window.SESSION_TOKEN}, body: '{}', keepalive: true}));
setInterval(poll, 250); poll();

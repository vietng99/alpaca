/* Alpaca vault: behaviour for the sign-in page (login.html) and the workspace hub
   (workspaces.html), in one classic script:
   binary rings around the code entry, the relay decrypt feed, the tactical backdrop (matrix rain,
   node web, CPU chips), the system-log ticker and the unlock takeover. Colors come from the
   --v-*-rgb tokens in vault.css, re-read on every theme change. All decoration is aria-hidden
   and honours prefers-reduced-motion. The only security decision is the server's: this page
   posts the code to /auth/login and the server sets the session cookie. */
(function () {
  'use strict';

  var root = document.documentElement;
  // Each page names its own theme key and default on <html>: the sign-in page defaults to dark
  // (key alpaca.login-theme), the hub to light and shares alpaca.theme with the cockpit.
  var THEME_KEY = root.getAttribute('data-theme-key') || 'alpaca.theme';
  var THEME_DEFAULT = root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  var MID = '\u00b7';

  function reduced() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
  }

  /* ---------- theme: stored choice wins, otherwise the page default ---------- */
  var storage = null;
  try { storage = window.localStorage; } catch (_) { storage = null; }
  function storedTheme() {
    try { var v = storage && storage.getItem(THEME_KEY); return v === 'dark' || v === 'light' ? v : null; } catch (_) { return null; }
  }
  function resolvedTheme() {
    var set = root.getAttribute('data-theme');
    if (set === 'dark' || set === 'light') return set;
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; } catch (_) { return 'light'; }
  }
  function announce() {
    var t = resolvedTheme();
    root.style.colorScheme = t;
    var buttons = document.querySelectorAll('[data-vault-theme]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(t === 'dark'));
      buttons[i].title = t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    }
    window.dispatchEvent(new CustomEvent('alpaca:themechange', {detail: {preference: t, resolved: t}}));
  }
  (function initTheme() {
    root.setAttribute('data-theme', storedTheme() || THEME_DEFAULT);
    document.addEventListener('click', function (event) {
      if (!event.target.closest || !event.target.closest('[data-vault-theme]')) return;
      var next = resolvedTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { storage && storage.setItem(THEME_KEY, next); } catch (_) { /* page setting still applies */ }
      announce();
    });
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', announce, {once: true});
    else announce();
  })();

  /* ---------- palette read from the CSS tokens ---------- */
  var pal = {acc: '20,184,230', hot: '61,214,255', glow: '168,239,255', ink: '225,238,246'};
  function readPalette() {
    var cs = getComputedStyle(root);
    function get(name, fallback) { var v = (cs.getPropertyValue(name) || '').trim(); return v || fallback; }
    pal.acc = get('--v-acc-rgb', pal.acc);
    pal.hot = get('--v-hot-rgb', pal.hot);
    pal.glow = get('--v-glow-rgb', pal.glow);
    pal.ink = get('--v-ink-rgb', pal.ink);
  }
  readPalette();
  window.addEventListener('alpaca:themechange', function () { readPalette(); });
  function rgba(rgb, a) { return 'rgba(' + rgb + ',' + a + ')'; }
  var rnd = function (a, b) { return a + Math.random() * (b - a); };

  /* ---------- binary rings: two rings per typed character, 12 at full code ----------
     Glyphs come from one small atlas (0/1 at four alpha levels) and are stamped per frame.
     Full-size ring sprites cost ~27M blended pixels a frame plus a texture upload on every
     re-bake at 12 rings; the atlas keeps both flat no matter how many rings are lit. */
  function mountRings(canvas, getCount) {
    var ctx = canvas.getContext('2d');
    if (!ctx) return function () {};
    var MAX = 12, PER = 2, SIZE = 1240, BASE_R = 300, GAP = 26, FONT = 13, CELL = 18, LEVELS = 4;
    var still = reduced();
    var dpr = Math.min(window.devicePixelRatio || 1, 1.25);
    canvas.width = SIZE * dpr; canvas.height = SIZE * dpr;
    var cx = SIZE / 2, cy = SIZE / 2, cellPx = Math.ceil(CELL * dpr), half = CELL / 2;
    var atlas = document.createElement('canvas');
    atlas.width = cellPx * 2 * LEVELS; atlas.height = cellPx;
    // Atlas cell index = glyph (0 or 1) * LEVELS + alpha level.
    function bakeAtlas() {
      var c = atlas.getContext('2d');
      c.setTransform(1, 0, 0, 1, 0, 0);
      c.clearRect(0, 0, atlas.width, atlas.height);
      c.font = (FONT * dpr) + 'px ui-monospace, "Cascadia Code", Consolas, monospace';
      c.textAlign = 'center'; c.textBaseline = 'middle';
      for (var g = 0; g < 2; g++) {
        for (var l = 0; l < LEVELS; l++) {
          c.fillStyle = rgba(pal.hot, 0.35 + 0.65 * (l + 0.5) / LEVELS);
          c.fillText(g ? '1' : '0', ((g * LEVELS + l) + 0.5) * cellPx, cellPx / 2);
        }
      }
    }
    bakeAtlas();
    function randomCell() { return (Math.random() < 0.5 ? 0 : LEVELS) + Math.floor(Math.random() * LEVELS); }
    var rings = [];
    for (var i = 0; i < MAX; i++) {
      var r = BASE_R + i * GAP, n = Math.max(24, Math.round((2 * Math.PI * r) / 13));
      var cos = new Float32Array(n), sin = new Float32Array(n), cells = new Uint8Array(n);
      for (var k = 0; k < n; k++) {
        var a = (k / n) * Math.PI * 2;
        cos[k] = Math.cos(a); sin[k] = Math.sin(a); cells[k] = randomCell();
      }
      rings.push({r: r, n: n, cos: cos, sin: sin, cells: cells, rot: Math.random() * Math.PI * 2,
        dir: i % 2 ? 1 : -1, prog: 0, flipAt: 0});
    }
    function flicker(ring) {
      for (var k = 0; k < ring.n; k++) if (Math.random() < 0.35) ring.cells[k] = randomCell();
    }
    window.addEventListener('alpaca:themechange', bakeAtlas);
    var raf = 0, last = 0;
    function frame(ts) {
      if (!last) last = ts;
      var dt = Math.min(0.05, (ts - last) / 1000); last = ts;
      var target = Math.min(MAX, getCount() * PER), complete = target >= MAX;
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (var i = 0; i < MAX; i++) {
        var ring = rings[i], tp = i < target ? 1 : 0;
        if (still) ring.prog = tp; else ring.prog += (tp - ring.prog) * Math.min(1, dt * 9);
        if (ring.prog < 0.02) continue;
        if (!still && ts >= ring.flipAt) { flicker(ring); ring.flipAt = ts + 170 + (i % 4) * 45; }
        if (!still) ring.rot += ring.dir * dt * 0.18;
        var newest = i >= target - PER && i < target;
        var alpha = 0.28 + 0.5 * ring.prog;
        if (newest) alpha *= 1.6;
        if (complete) alpha *= 1.25;
        if (!still) alpha *= 0.88 + 0.12 * Math.sin((ts / 1000) * 2.6 + i * 1.7);
        ctx.globalAlpha = Math.min(1, alpha);
        var s = 0.88 + 0.12 * ring.prog, R = ring.r * s, m = dpr * s;
        var cr = Math.cos(ring.rot), sr = Math.sin(ring.rot);
        for (var k = 0; k < ring.n; k++) {
          // Angle addition keeps trig out of the inner loop; the glyph faces along the ring.
          var ca = ring.cos[k] * cr - ring.sin[k] * sr, sa = ring.sin[k] * cr + ring.cos[k] * sr;
          ctx.setTransform(-sa * m, ca * m, -ca * m, -sa * m, (cx + ca * R) * dpr, (cy + sa * R) * dpr);
          ctx.drawImage(atlas, ring.cells[k] * cellPx, 0, cellPx, cellPx, -half, -half, CELL, CELL);
        }
      }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
    return function () { cancelAnimationFrame(raf); window.removeEventListener('alpaca:themechange', bakeAtlas); };
  }

  /* ---------- relay decrypt feed (top left): neutral link telemetry, no place names ---------- */
  var LINKS_A = [
    ['SAT 01', 'Uplink', 'Nominal'], ['SAT 02', 'Downlink', 'Nominal'], ['SAT 03', 'Crosslink', 'Locked'],
    ['SAT 04', 'Uplink', 'Nominal'], ['SAT 05', 'Beacon', 'Steady'], ['SAT 06', 'Downlink', 'Locked'],
    ['SAT 07', 'Crosslink', 'Nominal'], ['SAT 08', 'Uplink', 'Steady'], ['SAT 09', 'Beacon', 'Nominal'],
    ['SAT 10', 'Downlink', 'Steady']];
  var LINKS_B = [
    ['Node 3A7F', 'Handshake', 'OK'], ['Node 91C2', 'Session', 'Sealed'], ['Node 5E08', 'Registry', 'Ready'],
    ['Node C4D1', 'Handshake', 'OK'], ['Node 0B6E', 'Session', 'Sealed'], ['Node 7A93', 'Throttle', 'Armed'],
    ['Node E215', 'Handshake', 'OK'], ['Node 48FD', 'Registry', 'Ready'], ['Node 2C5B', 'Session', 'Sealed'],
    ['Node D90A', 'Throttle', 'Armed']];
  var RELAY_GLYPHS = '0123456789ABCDEF#%&$/<>*+=?!';
  function mountRelay(host) {
    var ENCRYPT_MS = 5000, PER_CHAR_MS = 55, HOLD_MS = 15000, TICK_MS = 50;
    var pools = [LINKS_A, LINKS_A, LINKS_A, LINKS_B, LINKS_B, LINKS_B];
    var fmt = function (e) { return e.join(' ' + MID + ' ').toUpperCase(); };
    var rg = function () { return RELAY_GLYPHS[(Math.random() * RELAY_GLYPHS.length) | 0]; };
    var scramble = function (t, n) { var o = ''; for (var i = 0; i < t.length; i++) o += i < n ? t[i] : rg(); return o; };
    host.innerHTML = '<div class="relayHead"><span class="relayHeadDot"></span>ALPACA-NET ' + MID +
      ' GLOBAL RELAY UPLINK</div><ul class="relayRows"></ul>';
    var list = host.querySelector('.relayRows');
    var rows = pools.map(function (pool, i) {
      var idx = i % pool.length, target = fmt(pool[idx]);
      var li = document.createElement('li'); li.className = 'relayRow';
      li.innerHTML = '<span class="relayDot"></span><span class="relayText"></span>';
      list.appendChild(li);
      return {pool: pool, idx: idx, target: target, phase: 'hold', elapsed: Math.max(0, HOLD_MS - 800 - i * 1100),
        display: target, dot: li.firstChild, text: li.lastChild};
    });
    function paint(r) { r.dot.setAttribute('data-phase', r.phase); r.text.setAttribute('data-phase', r.phase); r.text.textContent = r.display; }
    rows.forEach(paint);
    if (reduced()) return function () {};
    var id = setInterval(function () {
      rows.forEach(function (r) {
        r.elapsed += TICK_MS;
        if (r.phase === 'encrypt') {
          if (r.elapsed >= ENCRYPT_MS) { r.phase = 'decrypt'; r.elapsed = 0; }
          r.display = scramble(r.target, 0);
        } else if (r.phase === 'decrypt') {
          var shown = Math.floor(r.elapsed / PER_CHAR_MS);
          if (shown >= r.target.length) { r.phase = 'hold'; r.elapsed = 0; r.display = r.target; }
          else r.display = scramble(r.target, shown);
        } else if (r.elapsed >= HOLD_MS) {
          var used = {}; used[r.idx] = 1;
          rows.forEach(function (o) { if (o !== r && o.pool === r.pool) used[o.idx] = 1; });
          var choices = r.pool.map(function (_, k) { return k; }).filter(function (k) { return !used[k]; });
          r.idx = choices.length ? choices[(Math.random() * choices.length) | 0] : r.idx;
          r.target = fmt(r.pool[r.idx]); r.phase = 'encrypt'; r.elapsed = 0; r.display = scramble(r.target, 0);
        }
        paint(r);
      });
    }, TICK_MS);
    return function () { clearInterval(id); };
  }

  /* ---------- tactical backdrop: matrix rain, node web, CPU chips ---------- */
  function mountBackdrop(canvas, variant) {
    var ctx = canvas.getContext('2d');
    if (!ctx) return function () {};
    var still = reduced(), ambient = variant === 'ambient', A = ambient ? 0.5 : 1;
    var GLYPHS = '0123456789<>[]{}=+*#$%&Alpaca'.split('');
    var pick = function () { return GLYPHS[(Math.random() * GLYPHS.length) | 0]; };
    var COL_W = ambient ? 24 : 18, FONT = 14, DIST = ambient ? 134 : 150;
    var w = 0, h = 0, raf = 0, last = 0, cols = [], nodes = [], links = [], pulses = [];
    function build() {
      var rect = canvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width; h = rect.height;
      canvas.width = Math.max(1, Math.round(w * dpr)); canvas.height = Math.max(1, Math.round(h * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.textAlign = 'center';
      cols = [];
      for (var i = 0; i < Math.ceil(w / COL_W); i++) {
        var len = Math.floor(rnd(5, 13)), g = [];
        for (var k = 0; k < len; k++) g.push(pick());
        cols.push({x: i * COL_W + COL_W / 2, y: rnd(-h, h), speed: ambient ? rnd(34, 92) : rnd(55, 140), len: len, glyphs: g});
      }
      var N = ambient ? Math.max(11, Math.min(22, Math.round((w * h) / 92000))) : Math.max(16, Math.min(32, Math.round((w * h) / 54000)));
      var V = ambient ? 6 : 10;
      nodes = [];
      for (var j = 0; j < N; j++) nodes.push({x: rnd(0, w), y: rnd(0, h), vx: rnd(-V, V), vy: rnd(-V, V), cpu: j % 6 === 0, ph: rnd(0, Math.PI * 2)});
      links = [];
      nodes.forEach(function (src, ci) {
        if (!src.cpu) return;
        nodes.map(function (n, j) { return {j: j, d: Math.pow(n.x - src.x, 2) + Math.pow(n.y - src.y, 2)}; })
          .filter(function (o) { return o.j !== ci; }).sort(function (a, b) { return a.d - b.d; }).slice(0, 3)
          .forEach(function (o) { links.push([ci, o.j]); });
      });
      for (var q = 0; q < N; q++) { var a = (Math.random() * N) | 0, b = (Math.random() * N) | 0; if (a !== b) links.push([a, b]); }
      pulses = [];
      for (var p = 0; p < Math.min(links.length, 16); p++) pulses.push({link: (Math.random() * links.length) | 0, t: Math.random(), speed: rnd(0.25, 0.6)});
    }
    function frame(ts) {
      if (!last) last = ts;
      var dt = Math.min(0.05, (ts - last) / 1000); last = ts;
      ctx.clearRect(0, 0, w, h);
      ctx.font = FONT + 'px ui-monospace, "Cascadia Code", Consolas, monospace';
      cols.forEach(function (c) {
        c.y += c.speed * dt;
        if (c.y - c.len * FONT > h) { c.y = rnd(-h * 0.5, 0); c.speed = ambient ? rnd(34, 92) : rnd(55, 140); }
        if (Math.random() < 0.06) c.glyphs[0] = pick();
        for (var k = 0; k < c.len; k++) {
          var gy = c.y - k * FONT;
          if (gy < -FONT || gy > h + FONT) continue;
          var a = 1 - k / c.len;
          ctx.fillStyle = k === 0 ? rgba(pal.glow, (0.85 * a + 0.12) * A) : rgba(pal.acc, 0.3 * a * A);
          ctx.fillText(c.glyphs[k], c.x, gy);
        }
      });
      nodes.forEach(function (n) {
        n.x += n.vx * dt; n.y += n.vy * dt;
        if (n.x < 0 || n.x > w) n.vx *= -1;
        if (n.y < 0 || n.y > h) n.vy *= -1;
        n.x = Math.max(0, Math.min(w, n.x)); n.y = Math.max(0, Math.min(h, n.y));
      });
      ctx.lineWidth = 1;
      for (var i = 0; i < nodes.length; i++) {
        for (var j = i + 1; j < nodes.length; j++) {
          var d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
          if (d < DIST) {
            ctx.strokeStyle = rgba(pal.hot, (1 - d / DIST) * 0.2 * A);
            ctx.beginPath(); ctx.moveTo(nodes[i].x, nodes[i].y); ctx.lineTo(nodes[j].x, nodes[j].y); ctx.stroke();
          }
        }
      }
      ctx.strokeStyle = rgba(pal.acc, 0.16 * A);
      links.forEach(function (l) { ctx.beginPath(); ctx.moveTo(nodes[l[0]].x, nodes[l[0]].y); ctx.lineTo(nodes[l[1]].x, nodes[l[1]].y); ctx.stroke(); });
      pulses.forEach(function (p) {
        p.t += p.speed * dt;
        if (p.t >= 1) { p.t = 0; p.link = (Math.random() * links.length) | 0; p.speed = rnd(0.25, 0.6); }
        var l = links[p.link] || links[0];
        if (!l) return;
        ctx.fillStyle = rgba(pal.glow, 0.9 * A);
        ctx.beginPath();
        ctx.arc(nodes[l[0]].x + (nodes[l[1]].x - nodes[l[0]].x) * p.t, nodes[l[0]].y + (nodes[l[1]].y - nodes[l[0]].y) * p.t, 1.7, 0, Math.PI * 2);
        ctx.fill();
      });
      var tsec = ts / 1000;
      nodes.forEach(function (n) {
        var glow;
        if (n.cpu) {
          var s = 9;
          ctx.fillStyle = rgba(pal.acc, 0.12 * A); ctx.strokeStyle = rgba(pal.hot, 0.7 * A); ctx.lineWidth = 1.2;
          ctx.beginPath(); ctx.rect(n.x - s, n.y - s, s * 2, s * 2); ctx.fill(); ctx.stroke();
          ctx.strokeStyle = rgba(pal.hot, 0.55 * A); ctx.lineWidth = 1;
          for (var k = -1; k <= 1; k++) {
            ctx.beginPath();
            ctx.moveTo(n.x + k * 5, n.y - s); ctx.lineTo(n.x + k * 5, n.y - s - 3);
            ctx.moveTo(n.x + k * 5, n.y + s); ctx.lineTo(n.x + k * 5, n.y + s + 3);
            ctx.moveTo(n.x - s, n.y + k * 5); ctx.lineTo(n.x - s - 3, n.y + k * 5);
            ctx.moveTo(n.x + s, n.y + k * 5); ctx.lineTo(n.x + s + 3, n.y + k * 5);
            ctx.stroke();
          }
          glow = 0.5 + 0.5 * Math.sin(tsec * 2 + n.ph);
          ctx.fillStyle = rgba(pal.hot, (0.4 + 0.5 * glow) * A);
          ctx.fillRect(n.x - 2.5, n.y - 2.5, 5, 5);
        } else {
          glow = 0.5 + 0.5 * Math.sin(tsec * 1.6 + n.ph);
          ctx.fillStyle = rgba(pal.glow, (0.35 + 0.4 * glow) * A);
          ctx.beginPath(); ctx.arc(n.x, n.y, 2, 0, Math.PI * 2); ctx.fill();
        }
      });
      if (!still) raf = requestAnimationFrame(frame);
    }
    build();
    raf = requestAnimationFrame(frame);
    var onResize = function () { build(); if (still) requestAnimationFrame(frame); };
    var onTheme = function () { if (still) requestAnimationFrame(frame); };
    window.addEventListener('resize', onResize);
    window.addEventListener('alpaca:themechange', onTheme);
    return function () { cancelAnimationFrame(raf); window.removeEventListener('resize', onResize); window.removeEventListener('alpaca:themechange', onTheme); };
  }

  /* ---------- system-log ticker ---------- */
  function mountSyslog(host, lines) {
    host.innerHTML = '<span class="syslogDot"></span><span class="syslogLine"></span>';
    var line = host.lastChild, i = 0;
    line.textContent = lines[0];
    if (reduced()) return;
    setInterval(function () {
      i = (i + 1) % lines.length;
      var fresh = line.cloneNode(false);
      fresh.textContent = lines[i];
      host.replaceChild(fresh, line);
      line = fresh;
    }, 2200);
  }

  /* ---------- the unlock takeover ---------- */
  var SCRAMBLE = '!<>-_/[]{}=+*?#0123456789$%&@';
  function vaultSvg() {
    var ticks = '';
    for (var i = 0; i < 12; i++) {
      var a = (i / 12) * Math.PI * 2 - Math.PI / 2;
      ticks += '<line x1="' + (100 + Math.cos(a) * 88).toFixed(1) + '" y1="' + (100 + Math.sin(a) * 88).toFixed(1) +
        '" x2="' + (100 + Math.cos(a) * 94).toFixed(1) + '" y2="' + (100 + Math.sin(a) * 94).toFixed(1) + '"/>';
    }
    return '<svg class="vault" viewBox="0 0 200 200" aria-hidden="true">' +
      '<defs><linearGradient id="vScan" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" class="v-scanstop" stop-opacity="0"/>' +
      '<stop offset="50%" class="v-scanstop" stop-opacity="0.55"/><stop offset="100%" class="v-scanstop" stop-opacity="0"/></linearGradient></defs>' +
      '<circle class="v-rim" cx="100" cy="100" r="94" stroke-width="1" fill="none"/>' +
      '<g class="ring ring1"><circle class="v-r1" cx="100" cy="100" r="82" stroke-width="2" stroke-dasharray="6 9" fill="none"/></g>' +
      '<g class="ring ring2"><circle class="v-r2" cx="100" cy="100" r="62" stroke-width="2" stroke-dasharray="3 14" fill="none"/></g>' +
      '<g class="ring ring3"><circle class="v-r3" cx="100" cy="100" r="44" stroke-width="2" stroke-dasharray="2 5" fill="none"/></g>' +
      '<g class="v-ticks" stroke-width="1.5" stroke-linecap="round">' + ticks + '</g>' +
      '<g><path class="shackle v-lock" d="M89 92 V82 a11 11 0 0 1 22 0 V92" stroke-width="2.5" fill="none" stroke-linecap="round"/>' +
      '<rect class="v-body v-lock" x="83" y="92" width="34" height="28" rx="3.5" stroke-width="2"/>' +
      '<circle class="v-key" cx="100" cy="103" r="3"/><rect class="v-key" x="98.5" y="105" width="3" height="9" rx="1"/></g>' +
      '<rect class="scan" x="14" y="98" width="172" height="4" rx="2" fill="url(#vScan)"/></svg>';
  }

  /* ---------- workspace handoff: the cockpit's first-paint reads load while the takeover plays ----------
     /hub/ opens on the cockpit route, which renders from these five reads (hub.js, cockpit()). Each
     response is parked in the Cache API under HANDOFF; hub.js takes it once on its first render and
     then deletes the cache, so later reads always go to the server. */
  var HANDOFF = 'alpaca-handoff', HANDOFF_WAIT_MS = 15000;
  var HUB_FIRST_PAINT = ['/hub/overview.json', '/hub/runs.json', '/hub/history.json?limit=6&kind=progress',
    '/board/data.json', '/data.json'];
  function prefetchWorkspace(href) {
    var url;
    try { url = new URL(href, location.href); } catch (_) { return Promise.resolve(); }
    if (url.origin !== location.origin || url.pathname.indexOf('/hub') !== 0 || !window.caches) return Promise.resolve();
    return caches.delete(HANDOFF).then(function () { return caches.open(HANDOFF); }).then(function (cache) {
      return Promise.all(HUB_FIRST_PAINT.map(function (path) {
        return fetch(path, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
          if (!r.ok) return;
          return r.blob().then(function (body) {
            return cache.put(path, new Response(body, {headers: {'Content-Type': 'application/json',
              'X-Handoff-At': String(Date.now())}}));
          });
        }).catch(function () { /* the cockpit reads it itself */ });
      }));
    }).catch(function () { /* no Cache API here: the cockpit loads as before */ });
  }
  // Settles when `ready` does, or after HANDOFF_WAIT_MS so a slow read never holds the takeover.
  function settle(ready) {
    return new Promise(function (resolve) {
      setTimeout(resolve, HANDOFF_WAIT_MS);
      Promise.resolve(ready).then(resolve, resolve);
    });
  }

  function takeover(opts) {
    var el = document.createElement('div');
    el.className = 'takeover';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'assertive');
    el.setAttribute('aria-label', opts.label || 'Granting access');
    el.innerHTML = '<canvas class="canvas-fill" aria-hidden="true"></canvas><div class="backdropVeil" aria-hidden="true"></div>' +
      '<div class="unlock"><div class="unlockBanner">' + opts.verb + ' <b></b></div>' +
      '<div class="unlockStatus"><span class="unlockCaret">&gt;</span> <span class="detail"></span><span class="unlockBlink">_</span></div>' +
      vaultSvg() + '<ul class="stages"></ul><div class="result"></div>' +
      '<div class="loading" hidden><div class="loadingLabel">' + (opts.loading || 'Loading workspace') +
      '<span class="unlockBlink">_</span></div><div class="loadBar" aria-hidden="true"><span class="loadFill"></span></div></div></div>';
    document.body.appendChild(el);
    mountBackdrop(el.querySelector('canvas'), 'full');
    var banner = el.querySelector('.unlockBanner b'), detail = el.querySelector('.detail');
    var list = el.querySelector('.stages'), svg = el.querySelector('.vault');
    var result = el.querySelector('.result'), loading = el.querySelector('.loading');
    var items = opts.stages.map(function (s) { var li = document.createElement('li'); li.className = 'stage'; li.textContent = s; list.appendChild(li); return li; });
    function setActive(n) {
      items.forEach(function (li, i) { li.className = 'stage' + (i < n ? ' stageDone' : i === n ? ' stageActive' : ''); });
      detail.textContent = n < 0 ? 'initializing secure session' : n >= items.length ? 'all checks passed ' + MID + ' opening vault' : opts.details[n];
    }
    var timers = [];
    var push = function (fn, ms) { timers.push(setTimeout(fn, ms)); };
    var handle = opts.handle, gate = settle(opts.ready);
    function finish() { gate.then(opts.done); }
    setActive(-1);
    if (reduced()) {
      banner.textContent = handle;
      push(function () { setActive(items.length); svg.classList.add('vaultOpen'); }, 0);
      push(function () { result.textContent = opts.result; result.classList.add('resultOn'); }, 360);
      push(function () { loading.hidden = false; }, 900);
      push(finish, 1200);
      return;
    }
    var STEP = opts.step || 320, base = 300, allDone = base + items.length * STEP, start = performance.now();
    (function tick() {
      var p = Math.min(1, (performance.now() - start) / 950), out = '';
      for (var i = 0; i < handle.length; i++) {
        var cp = p * handle.length - i;
        out += cp >= 1 ? handle[i] : cp < 0 ? ' ' : SCRAMBLE[(Math.random() * SCRAMBLE.length) | 0];
      }
      banner.textContent = out;
      if (p < 1) requestAnimationFrame(tick); else banner.textContent = handle;
    })();
    items.forEach(function (_, i) { push(function () { setActive(i); }, base + i * STEP); });
    push(function () { setActive(items.length); }, allDone);
    push(function () { svg.classList.add('vaultOpen'); }, allDone + 120);
    push(function () { result.textContent = opts.result; result.classList.add('resultOn'); }, allDone + 560);
    push(function () { loading.hidden = false; }, allDone + 980);
    push(finish, allDone + 1300);
  }

  /* ---------- sign-in page ---------- */
  var LOGIN_STAGES = ['ESTABLISHING SECURE CHANNEL', 'VERIFYING ACCESS CODE', 'SIGNING SESSION TOKEN',
    'LOADING WORKSPACE REGISTRY', 'AUTHORIZING CLEARANCE'];
  var LOGIN_DETAILS = ['request received ' + MID + ' channel open', 'access code matched ' + MID + ' constant-time compare',
    'HMAC-SHA256 session cookie ' + MID + ' 12 h', 'workspace list resolved', 'operator access authorized'];

  function destination() {
    if (location.pathname.indexOf('/login') === 0) {
      var next = new URLSearchParams(location.search).get('next') || '/';
      return next.charAt(0) === '/' && next.charAt(1) !== '/' ? next : '/';
    }
    return location.pathname + location.search + location.hash;   // the locked page itself
  }

  function initLogin() {
    var globe = document.getElementById('globe');
    if (globe && window.AlpacaGlobe) window.AlpacaGlobe.mount(globe);
    var relay = document.getElementById('relay');
    if (relay) mountRelay(relay);
    mountSyslog(document.getElementById('syslog'), ['secure channel established', 'awaiting operator credentials',
      'session vault ' + MID + ' sealed', 'ALPACA-NET relay ' + MID + ' online', 'record writer ' + MID + ' alpaca only',
      'failure throttle ' + MID + ' armed', 'workspace registry ' + MID + ' nominal']);

    var form = document.getElementById('login-form'), code = document.getElementById('code'), user = document.getElementById('user');
    var userField = document.getElementById('user-field'), card = document.getElementById('card');
    var error = document.getElementById('error'), submit = document.getElementById('submit');
    mountRings(document.getElementById('rings'), function () { return code.value.length; });

    fetch('/login/info.json', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (info) {
      if (info.signed_in) { location.replace(destination()); return; }
      if (info.user) userField.hidden = false;
      if (!info.configured) fail('No web login is configured on this server. Set .alpaca/files-auth first.');
    }).catch(function () { /* the form still works; the server answers on submit */ });

    function fail(message) {
      error.textContent = message;
      error.hidden = false;
      card.classList.remove('denied');
      void card.offsetWidth;          // restart the shake on consecutive errors
      card.classList.add('denied');
      setTimeout(function () { card.classList.remove('denied'); }, 420);
    }
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      error.hidden = true;
      submit.disabled = true; submit.classList.add('busy');
      var started = Date.now();
      fetch('/auth/login', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user: user.value.trim(), code: code.value})})
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { return {status: r.status, body: b}; }); })
        .then(function (res) {
          // Hold the spinner a beat so the loading state reads as a state, not a flicker.
          setTimeout(function () {
            submit.disabled = false; submit.classList.remove('busy');
            if (res.status === 200 && res.body.ok) {
              takeover({verb: 'AUTHENTICATING', handle: (user.value.trim() || 'OPERATOR').toUpperCase(),
                stages: LOGIN_STAGES, details: LOGIN_DETAILS, result: 'ACCESS GRANTED',
                ready: prefetchWorkspace(destination()),
                done: function () { location.replace(destination()); }});
            } else {
              code.value = '';
              fail(res.body.error || 'Sign-in failed (' + res.status + ').');
              code.focus();
            }
          }, Math.max(0, 450 - (Date.now() - started)));
        })
        .catch(function () {
          submit.disabled = false; submit.classList.remove('busy');
          fail('The server did not answer. Check the connection and try again.');
        });
    });
    code.focus();
  }

  /* ---------- workspace hub ---------- */
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

  function ago(iso) {
    var t = Date.parse(iso);
    if (!t) return '';
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return Math.round(s) + ' s ago';
    if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 172800) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function summarize(data) {
    var pulse = data.pulse || {}, now = pulse.now || {}, tasks = pulse.tasks || {};
    var ops = (now.ops || []).filter(function (o) { return o.status === 'open'; });
    var op = ops[0] || null;
    var work = ((now.sessions || {}).work || []).filter(function (s) { return s.active; });
    return {level: now.level || '', op: op, opsOpen: pulse.ops_open || 0, open: tasks.open || 0, doing: tasks.doing || 0,
      done: tasks.done || 0, active: work.length, next: now.next_action || '', asOf: now.as_of || data.generated || ''};
  }

  function fillTile(tile, s) {
    var live = tile.querySelector('.tile-live');
    live.classList.toggle('on', s.active > 0);
    live.lastChild.textContent = s.active ? s.active + ' live' : 'idle';
    var op = tile.querySelector('.tile-op');
    op.classList.remove('skeleton');
    op.textContent = '';
    if (s.op) {
      op.appendChild(el('b', null, s.op.id));
      op.appendChild(document.createTextNode(s.op.title || s.op.done_when || ''));
      var total = s.op.rows_total || s.op.tasks_total || 0;
      var done = s.op.rows_total ? s.op.rows_done : s.op.tasks_done;
      var pct = total ? Math.round((100 * (done || 0)) / total) : 0;
      tile.querySelector('.bar span').style.width = pct + '%';
      tile.querySelector('.bar-note').textContent = total ? (done || 0) + ' of ' + total + (s.op.rows_total ? ' rows discharged' : ' tasks done') +
        (s.op.rows_blocked ? ' ' + MID + ' ' + s.op.rows_blocked + ' blocked' : '') : 'no rows or tasks yet';
    } else {
      op.textContent = 'No open operation.';
      tile.querySelector('.bar-note').textContent = '';
    }
    var stats = tile.querySelectorAll('.stat b');
    stats[0].textContent = s.opsOpen; stats[1].textContent = s.doing; stats[2].textContent = s.open; stats[3].textContent = s.done;
    var next = tile.querySelector('.tile-next');
    next.textContent = '';
    next.appendChild(el('em', null, 'next '));
    next.appendChild(document.createTextNode(s.next || 'nothing queued'));
    tile.querySelector('.tile-open small').textContent = (s.level ? 'level ' + s.level + ' ' + MID + ' ' : '') + (s.asOf ? 'updated ' + ago(s.asOf) : '');
  }

  function buildTile(ws) {
    var a = el('a', 'card tile');
    a.href = ws.href;
    a.setAttribute('aria-label', 'Open the ' + ws.name + ' ' + (ws.kind || 'Operations workspace').toLowerCase());
    a.innerHTML = '<span class="runBorder" aria-hidden="true"></span><span class="cardScan" aria-hidden="true"></span>' +
      '<div class="tile-top"><span class="tile-mark" aria-hidden="true"></span><div><div class="tile-name"></div><div class="tile-kind"></div></div>' +
      '<span class="tile-live"><i></i><span>checking</span></span></div>' +
      '<p class="tile-op skeleton">Reading the record</p><div class="bar"><span></span></div><div class="bar-note"></div>' +
      '<div class="stats"><div class="stat"><b>-</b><span>Open ops</span></div><div class="stat"><b>-</b><span>Doing</span></div>' +
      '<div class="stat"><b>-</b><span>Queued</span></div><div class="stat"><b>-</b><span>Done</span></div></div>' +
      '<div class="tile-next"></div><div class="tile-open"><span>Open workspace &rarr;</span><small></small></div>';
    a.querySelector('.tile-mark').textContent = ws.name.charAt(0);
    a.querySelector('.tile-name').textContent = ws.name;
    a.querySelector('.tile-kind').textContent = (ws.kind || 'Operations workspace') + ' ' + MID + ' ' + (ws.project || ws.id || '');
    a.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button === 1) return;   // new tab: let it go
      event.preventDefault();
      takeover({verb: 'OPENING', handle: ws.name, label: 'Opening workspace', step: 180, result: 'WORKSPACE READY',
        stages: ['RESOLVING WORKSPACE', 'ATTACHING TO RECORD', 'LOADING COCKPIT'],
        details: [(ws.project || ws.id || '') + ' ' + MID + ' ' + (ws.kind || 'Operations workspace').toLowerCase(), 'read-only view of .alpaca/alpaca.db', ws.href],
        ready: prefetchWorkspace(ws.href),
        done: function () { location.href = ws.href; }});
    });
    return a;
  }

  function refreshTile(tile, ws) {
    var op = tile.querySelector('.tile-op');
    if (!ws.summary) {
      op.classList.remove('skeleton');
      op.textContent = 'No live summary for this workspace. Open it to see its cockpit.';
      return Promise.resolve();
    }
    return fetch(ws.summary, {credentials: 'same-origin', cache: 'no-cache'}).then(function (r) {
      // A 401 here is this hub's own sign-in lapsing. Another instance's tile is read by the
      // server as reachability only (/workspaces/summary.json: {reachable}), so it never signs
      // this page out and nothing of that instance's record reaches this page.
      if (r.status === 401) { location.replace('/login?next=' + encodeURIComponent(location.pathname)); throw Error('signed out'); }
      if (!r.ok) throw Error('status ' + r.status);
      return r.json();
    }).then(function (data) {
      if (ws.self !== false) { fillTile(tile, summarize(data)); return; }
      op.classList.remove('skeleton');
      var live = tile.querySelector('.tile-live');
      live.classList.toggle('on', !!(data && data.reachable));
      live.lastChild.textContent = data && data.reachable ? 'up' : 'down';
      op.textContent = data && data.reachable
        ? 'Workspace is up. Open it and sign in there to see its cockpit.'
        : 'Workspace not answering. It may be stopped.';
    }).catch(function (err) {
      if (err.message === 'signed out') return;
      op.classList.remove('skeleton');
      op.textContent = 'Live summary unavailable (' + err.message + '). The workspace may still open.';
    });
  }

  function initHub() {
    mountBackdrop(document.getElementById('backdrop'), 'ambient');
    mountSyslog(document.getElementById('syslog'), ['session vault ' + MID + ' open', 'workspace registry ' + MID + ' loaded',
      'record writer ' + MID + ' alpaca only', 'live summaries ' + MID + ' 15 s refresh']);
    var grid = document.getElementById('grid');
    document.getElementById('signout').addEventListener('click', function () {
      fetch('/auth/logout', {method: 'POST', credentials: 'same-origin'}).finally(function () { location.replace('/login'); });
    });
    fetch('/workspaces.json', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (r.status === 401) { location.replace('/login'); throw Error('signed out'); }
      return r.json();
    }).then(function (body) {
      grid.textContent = '';
      var list = body.workspaces || [];
      document.getElementById('count').textContent = list.length + (list.length === 1 ? ' workspace' : ' workspaces');
      var tiles = list.map(function (ws) { var t = buildTile(ws); grid.appendChild(t); return {tile: t, ws: ws}; });
      if (list.length < 2) {
        var ghost = el('div', 'tile-ghost');
        ghost.innerHTML = '<b>More workspaces</b><p>Each instance registered on this host gets its own tile here. ' +
          'Register another with <code>alpaca workspace add</code> (docs/workspaces.md).</p>';
        grid.appendChild(ghost);
      }
      function all() { tiles.forEach(function (x) { refreshTile(x.tile, x.ws); }); }
      all();
      setInterval(function () { if (!document.hidden) all(); }, 15000);
      if (tiles[0]) tiles[0].tile.focus({preventScroll: true});
    }).catch(function (err) {
      if (err.message !== 'signed out') grid.textContent = 'Could not load the workspace list: ' + err.message;
    });
  }

  function boot() {
    var page = document.body.getAttribute('data-page');
    if (page === 'login') initLogin();
    else if (page === 'hub') initHub();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
  else boot();
})();

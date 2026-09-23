/* ============================================================================
   vault-globe.js - a tactical rotating world globe (orthographic) drawn on a
   canvas behind the sign-in page. It is a neutral world: it marks no place,
   names no city, draws no route and highlights no country. What it draws:
   coastlines, graticule, a starfield, a satellite fleet on screen orbits, a
   tidally-locked Moon and an occasional comet. Dependency-free: hand-rolled
   projection + tiny TopoJSON decoder; coastline data is the vendored
   countries-110m.json (same origin only; a graticule globe when it is
   missing). Honours prefers-reduced-motion.

   Colours come from CSS custom properties on document.documentElement, each an
   "r,g,b" string: --v-acc-rgb, --v-hot-rgb, --v-glow-rgb, --v-ink-rgb. They are
   re-read whenever window fires 'alpaca:themechange'.

   Usage:  var stop = window.AlpacaGlobe.mount(canvasEl);  ...  stop();
   ============================================================================ */
(function () {
  'use strict';

  var DEG = Math.PI / 180;

  var PAL_VARS = {
    acc: ['--v-acc-rgb', '22,199,245'],
    hot: ['--v-hot-rgb', '61,220,255'],
    glow: ['--v-glow-rgb', '168,240,255'],
    ink: ['--v-ink-rgb', '235,244,250']
  };

  /* Scale an "r,g,b" string toward black (used for the dark lunar maria and
     the moon's shading end stop, which were darker shades of the accent). */
  function shade(rgb, k) {
    var parts = String(rgb).split(/[\s,]+/).filter(function (s) { return s !== ''; });
    var out = [];
    for (var i = 0; i < 3; i++) {
      var v = parseFloat(parts[i]);
      if (!isFinite(v)) v = 0;
      out.push(Math.max(0, Math.min(255, Math.round(v * k))));
    }
    return out.join(',');
  }

  function readPalette() {
    var cs = null;
    try {
      cs = window.getComputedStyle(document.documentElement);
    } catch (e) {
      cs = null;
    }
    var pal = {};
    for (var key in PAL_VARS) {
      if (!Object.prototype.hasOwnProperty.call(PAL_VARS, key)) continue;
      var v = '';
      if (cs) {
        try {
          v = String(cs.getPropertyValue(PAL_VARS[key][0]) || '').trim();
        } catch (e2) {
          v = '';
        }
      }
      pal[key] = v || PAL_VARS[key][1];
    }
    pal.accDim = shade(pal.acc, 0.5); // was 120,8,30 (moon shading end stop)
    pal.accDeep = shade(pal.acc, 0.3); // was 70,2,20 (lunar maria)
    return pal;
  }

  /* Satellite fleet - deterministic tilted elliptical screen-orbits. */
  var SAT_N = 9;
  var SATS = [];
  for (var si = 0; si < SAT_N; si++) {
    SATS.push({
      a0: (si / SAT_N) * Math.PI * 2,
      speed: 0.08 + (si % 4) * 0.035,
      rx: 1.05 + (si % 3) * 0.08,
      ry: 0.18 + ((si * 3) % 5) * 0.07,
      tilt: ((si * 40) % 180) * DEG,
      dir: si % 2 ? 1 : -1
    });
  }

  /* -- Moon - a tidally-locked satellite on a prograde, slightly-inclined
     orbit, drawn in the same faint wireframe-globe style as Earth (a small
     mini-globe, not a photoreal Moon, kept subtle so it does not dominate).
     Astrophysics kept:
     - Prograde revolution: same rotational sense as Earth's spin.
     - Tidal lock: spin period == orbital period, so from this external
       (inertial) camera its surface turns exactly once per orbit and the same
       face stays Earthward. (The Moon's own rotation angle == its orbital
       angle.)
     - Slight eccentricity + inclination.
     - Depth: the orbit plane tilts toward the viewer, so it passes BEHIND the
       globe on the far half and IN FRONT on the near half.
     Orbital distance is compressed to keep it in frame - the one liberty. */
  var MOON = {
    dist: 1.2, // orbital semi-major axis, in Earth radii (compressed for framing)
    rad: 0.16, // Moon globe radius, in Earth radii (kept small + subtle)
    ecc: 0.055, // ~real lunar orbital eccentricity (subtle)
    incl: 42 * DEG, // view inclination of the orbit plane: depth + vertical squash
    node: -14 * DEG, // on-screen tilt of the orbit (echoes Earth's axial tilt)
    period: 14 // seconds per revolution (brisk)
  };
  /* Soft maria (dark patches) fixed to the Moon's surface - they drift with
     the tidal-locked spin to hint at rotation, with no hard edges. lon/lat in
     radians, r as a fraction of the Moon's radius. */
  var MOON_MARE = [
    { lon: 0.4, lat: 0.26, r: 0.52 },
    { lon: -0.85, lat: -0.16, r: 0.42 },
    { lon: 1.5, lat: -0.42, r: 0.34 }
  ];

  function mount(canvas) {
    var noop = function () {};
    if (!canvas || typeof canvas.getContext !== 'function') return noop;
    var ctx = canvas.getContext('2d');
    if (!ctx) return noop;

    var pal = readPalette();

    var reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    var TILT = -20 * DEG;
    var w = 0;
    var h = 0;
    var cx = 0;
    var cy = 0;
    var R = 0;
    var rot = -20;
    var raf = 0;
    var last = 0;
    var landRings = [];
    var stars = [];
    var alive = true;

    function rgba(rgb, a) {
      return 'rgba(' + rgb + ',' + a + ')';
    }

    function project(lon, lat) {
      var la = (lon + rot) * DEG;
      var ph = lat * DEG;
      var x = Math.cos(ph) * Math.sin(la);
      var y0 = Math.sin(ph);
      var z0 = Math.cos(ph) * Math.cos(la);
      var y = y0 * Math.cos(TILT) - z0 * Math.sin(TILT);
      var z = y0 * Math.sin(TILT) + z0 * Math.cos(TILT);
      return { x: cx + x * R, y: cy - y * R, z: z, vis: z > 0 };
    }

    function satPos(i, satTime) {
      var sat = SATS[i];
      var a = sat.a0 + sat.dir * satTime * sat.speed;
      var ex = Math.cos(a) * sat.rx * R;
      var ey = Math.sin(a) * sat.ry * R;
      var x = cx + ex * Math.cos(sat.tilt) - ey * Math.sin(sat.tilt);
      var y = cy + ex * Math.sin(sat.tilt) + ey * Math.cos(sat.tilt);
      return { x: x, y: y, z: 1, vis: true };
    }

    function strokeRings(rings, style, lw) {
      ctx.strokeStyle = style;
      ctx.lineWidth = lw;
      for (var r = 0; r < rings.length; r++) {
        var ring = rings[r];
        var pen = false;
        ctx.beginPath();
        for (var j = 0; j < ring.length; j++) {
          var p = project(ring[j][0], ring[j][1]);
          if (p.vis) {
            if (pen) ctx.lineTo(p.x, p.y);
            else ctx.moveTo(p.x, p.y);
            pen = true;
          } else pen = false;
        }
        ctx.stroke();
      }
    }

    /* Fill each ring from its visible points only - points on the far side of
       an orthographic globe project MIRRORED inside the disc, so including
       them corrupts the shape. */
    function fillRings(rings, style) {
      ctx.fillStyle = style;
      for (var r = 0; r < rings.length; r++) {
        var ring = rings[r];
        var visCount = 0;
        var started = false;
        ctx.beginPath();
        for (var j = 0; j < ring.length; j++) {
          var p = project(ring[j][0], ring[j][1]);
          if (!p.vis) continue;
          visCount++;
          if (started) ctx.lineTo(p.x, p.y);
          else {
            ctx.moveTo(p.x, p.y);
            started = true;
          }
        }
        if (visCount > 2) {
          ctx.closePath();
          ctx.fill();
        }
      }
    }

    var satT = 0;

    function rnd(a, b) {
      return a + Math.random() * (b - a);
    }

    // -- starfield - outside the globe disc so the wireframe stays clean --
    function drawStars(tsec) {
      for (var i = 0; i < stars.length; i++) {
        var st = stars[i];
        var tw = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(tsec * st.f + st.ph);
        ctx.fillStyle = rgba(pal.ink, 0.08 + 0.2 * tw);
        ctx.fillRect(st.x, st.y, st.r, st.r);
      }
    }

    // -- Moon - prograde, tidally-locked, sunlit; passes behind/in front. --
    function moonState(tsec) {
      var theta = (tsec / MOON.period) * Math.PI * 2; // prograde mean angle
      var r = MOON.dist * (1 - MOON.ecc * Math.cos(theta)); // ~elliptical orbit
      var ox = Math.cos(theta) * r;
      var oy = Math.sin(theta) * r;
      // tilt the orbital plane toward the viewer: vertical squash + depth (z)
      var px = ox;
      var py = oy * Math.cos(MOON.incl);
      var pz = oy * Math.sin(MOON.incl); // +z: in front of Earth
      // rotate the on-screen ellipse by the orbit's node angle
      var cn = Math.cos(MOON.node);
      var sn = Math.sin(MOON.node);
      var ex = px * cn - py * sn;
      var ey = px * sn + py * cn;
      return { x: cx + ex * R, y: cy - ey * R, z: pz, theta: theta };
    }

    function drawMoonOrbit() {
      ctx.save();
      ctx.beginPath();
      for (var i = 0; i <= 100; i++) {
        var th = (i / 100) * Math.PI * 2;
        var r = MOON.dist * (1 - MOON.ecc * Math.cos(th));
        var px = Math.cos(th) * r;
        var py = Math.sin(th) * r * Math.cos(MOON.incl);
        var cn = Math.cos(MOON.node);
        var sn = Math.sin(MOON.node);
        var X = cx + (px * cn - py * sn) * R;
        var Y = cy - (px * sn + py * cn) * R;
        if (i === 0) ctx.moveTo(X, Y);
        else ctx.lineTo(X, Y);
      }
      ctx.setLineDash([2, 8]);
      ctx.strokeStyle = rgba(pal.acc, 0.1);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    function paintMoon(m) {
      var mr = MOON.rad * R;
      var spin = m.theta; // tidal lock: surface turns once per orbit

      ctx.save();
      ctx.beginPath();
      ctx.arc(m.x, m.y, mr, 0, Math.PI * 2);
      ctx.clip();

      // faint full-disc base so the whole sphere reads (like Earth's translucent fill)
      ctx.fillStyle = rgba(pal.acc, 0.05);
      ctx.fillRect(m.x - mr, m.y - mr, mr * 2, mr * 2);

      // smooth directional shading: a lit sphere, bright upper-left, dim lower-right
      var g = ctx.createRadialGradient(
        m.x - mr * 0.4,
        m.y - mr * 0.44,
        mr * 0.05,
        m.x - mr * 0.05,
        m.y - mr * 0.05,
        mr * 1.18
      );
      g.addColorStop(0, rgba(pal.hot, 0.44));
      g.addColorStop(0.45, rgba(pal.acc, 0.15));
      g.addColorStop(1, rgba(pal.accDim, 0));
      ctx.fillStyle = g;
      ctx.fillRect(m.x - mr, m.y - mr, mr * 2, mr * 2);

      // soft maria drifting with the tidal-locked spin (no hard edges)
      for (var i = 0; i < MOON_MARE.length; i++) {
        var c = MOON_MARE[i];
        var lon = c.lon + spin;
        var cl = Math.cos(lon);
        if (cl <= 0.05) continue; // on the far side
        var sx = m.x + Math.sin(lon) * Math.cos(c.lat) * mr;
        var sy = m.y - Math.sin(c.lat) * mr;
        var rad = c.r * mr * (0.45 + 0.55 * cl); // foreshorten near the limb
        var mg = ctx.createRadialGradient(sx, sy, 0, sx, sy, rad);
        mg.addColorStop(0, rgba(pal.accDeep, 0.16 * cl));
        mg.addColorStop(1, rgba(pal.accDeep, 0));
        ctx.fillStyle = mg;
        ctx.fillRect(m.x - mr, m.y - mr, mr * 2, mr * 2);
      }

      ctx.restore();

      // crisp limb
      ctx.beginPath();
      ctx.arc(m.x, m.y, mr, 0, Math.PI * 2);
      ctx.strokeStyle = rgba(pal.acc, 0.3);
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    // -- Comet fly-by - appears occasionally; tail always points anti-solar --
    var comet = {
      active: false,
      t: 0,
      dur: 3,
      cool: reduced ? 1e9 : 3 + Math.random() * 3, // first fly-by sooner
      a0: 0, // arc start bearing (around Earth's centre)
      a1: 0, // arc end bearing
      rc: 0, // arc radius (px) - just outside the limb
      size: 1
    };
    function spawnComet() {
      // a concentric arc that hugs the Earth's limb, entering from a varied
      // bearing each time and sweeping either way around the globe
      var span = rnd(90, 180) * DEG;
      var dir = Math.random() < 0.5 ? 1 : -1;
      var ac = rnd(100, 260) * DEG; // closest-approach bearing (visible side)
      comet.a0 = ac - (dir * span) / 2;
      comet.a1 = ac + (dir * span) / 2;
      comet.rc = R * rnd(1.05, 1.22);
      comet.dur = rnd(2.6, 4.2);
      comet.size = rnd(0.85, 1.35);
      comet.t = 0;
      comet.active = true;
    }
    function updateComet(dt) {
      if (reduced) return;
      if (!comet.active) {
        comet.cool -= dt;
        if (comet.cool <= 0) spawnComet();
        return;
      }
      comet.t += dt / comet.dur;
      if (comet.t >= 1) {
        comet.active = false;
        comet.cool = 8 + Math.random() * 4; // 8-12s between fly-bys
      }
    }
    function drawComet() {
      if (!comet.active) return;
      var t = comet.t;
      var a = comet.a0 + (comet.a1 - comet.a0) * t; // current bearing on the arc
      var bright = Math.min(1, Math.min(t, 1 - t) / 0.16); // ease in/out at edges
      if (bright <= 0) return;
      var s = comet.size;
      var rc = comet.rc;
      var sweep = Math.sign(comet.a1 - comet.a0) || 1; // travel direction on arc
      var hx = cx + Math.cos(a) * rc;
      var hy = cy + Math.sin(a) * rc;

      // tail trails BACK along the same arc, so it curves with the Earth's
      // limb. Thickness is applied radially (in/out from Earth's centre),
      // tapering to 0.
      var tailSpan = (20 + 18 * bright) * DEG * (0.7 + 0.5 * s);
      var N = 16;
      var hw = 4.5 * s * (0.7 + 0.3 * bright); // radial half-thickness at head
      var i, f, ai, wr;
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      ctx.beginPath();
      for (i = 0; i <= N; i++) {
        f = i / N;
        ai = a - sweep * tailSpan * f;
        wr = hw * (1 - f);
        ctx.lineTo(cx + Math.cos(ai) * (rc + wr), cy + Math.sin(ai) * (rc + wr));
      }
      for (i = N; i >= 0; i--) {
        f = i / N;
        ai = a - sweep * tailSpan * f;
        wr = hw * (1 - f);
        ctx.lineTo(cx + Math.cos(ai) * (rc - wr), cy + Math.sin(ai) * (rc - wr));
      }
      ctx.closePath();
      var tipA = a - sweep * tailSpan;
      var grad = ctx.createLinearGradient(
        hx,
        hy,
        cx + Math.cos(tipA) * rc,
        cy + Math.sin(tipA) * rc
      );
      grad.addColorStop(0, rgba(pal.ink, 0.55 * bright));
      grad.addColorStop(0.4, rgba(pal.glow, 0.22 * bright));
      grad.addColorStop(1, rgba(pal.glow, 0));
      ctx.fillStyle = grad;
      ctx.fill();

      // coma + nucleus
      var coma = ctx.createRadialGradient(hx, hy, 0, hx, hy, 9 * s);
      coma.addColorStop(0, rgba(pal.ink, 0.95 * bright));
      coma.addColorStop(0.4, rgba(pal.ink, 0.45 * bright));
      coma.addColorStop(1, rgba(pal.glow, 0));
      ctx.fillStyle = coma;
      ctx.beginPath();
      ctx.arc(hx, hy, 9 * s, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = rgba(pal.ink, bright);
      ctx.beginPath();
      ctx.arc(hx, hy, 1.8 * s, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    function build() {
      var rect = canvas.getBoundingClientRect();
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width;
      h = rect.height;
      canvas.width = Math.max(1, Math.round(w * dpr));
      canvas.height = Math.max(1, Math.round(h * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx = w * (w > 900 ? 0.6 : 0.5);
      cy = h * 0.48;
      R = Math.min(w, h) * (w > 900 ? 0.52 : 0.44); // zoomed in closer
      stars = [];
      var starN = Math.round((w * h) / 16000);
      for (var i = 0; i < starN; i++) {
        var x = Math.random() * w;
        var y = Math.random() * h;
        if (Math.hypot(x - cx, y - cy) < R + 10) continue;
        stars.push({
          x: x,
          y: y,
          r: 0.6 + Math.random() * 1.1,
          ph: Math.random() * Math.PI * 2,
          f: 0.4 + Math.random() * 1.3
        });
      }
    }

    function drawGraticule() {
      ctx.strokeStyle = rgba(pal.acc, 0.06);
      ctx.lineWidth = 1;
      var lat, lon, pen, p;
      for (lat = -60; lat <= 60; lat += 30) {
        pen = false;
        ctx.beginPath();
        for (lon = -180; lon <= 180; lon += 4) {
          p = project(lon, lat);
          if (p.vis) {
            if (pen) ctx.lineTo(p.x, p.y);
            else ctx.moveTo(p.x, p.y);
            pen = true;
          } else pen = false;
        }
        ctx.stroke();
      }
      for (lon = -180; lon < 180; lon += 30) {
        pen = false;
        ctx.beginPath();
        for (lat = -90; lat <= 90; lat += 4) {
          p = project(lon, lat);
          if (p.vis) {
            if (pen) ctx.lineTo(p.x, p.y);
            else ctx.moveTo(p.x, p.y);
            pen = true;
          } else pen = false;
        }
        ctx.stroke();
      }
    }

    function drawSatGlyph(p, bright) {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.strokeStyle = rgba(pal.hot, bright ? 0.95 : 0.6);
      ctx.lineWidth = 1.1;
      ctx.strokeRect(-2.5, -1.8, 5, 3.6);
      ctx.beginPath();
      ctx.moveTo(-8, 0);
      ctx.lineTo(-2.5, 0);
      ctx.moveTo(2.5, 0);
      ctx.lineTo(8, 0);
      ctx.stroke();
      ctx.restore();
    }

    function frame(ts) {
      if (!alive) return;
      if (!last) last = ts;
      var dt = (ts - last) / 1000;
      last = ts;
      if (dt > 0.05) dt = 0.05;
      var tsec = ts / 1000;
      satT += dt;
      updateComet(dt);

      rot -= dt * 6;

      ctx.clearRect(0, 0, w, h);

      drawStars(tsec);

      // Moon - compute once. Far half is drawn now (occluded by the globe
      // limb); the near half is drawn later, over the globe.
      var moon = moonState(tsec);
      drawMoonOrbit();
      if (moon.z < 0) {
        ctx.save();
        // clip OUT the Earth disk (even-odd) so the Moon ducks behind the limb
        ctx.beginPath();
        ctx.rect(0, 0, w, h);
        ctx.arc(cx, cy, R - 0.5, 0, Math.PI * 2);
        ctx.clip('evenodd');
        paintMoon(moon);
        ctx.restore();
      }

      // sphere base
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      var g = ctx.createRadialGradient(cx - R * 0.3, cy - R * 0.35, R * 0.1, cx, cy, R);
      g.addColorStop(0, rgba(pal.acc, 0.07));
      g.addColorStop(0.7, 'rgba(10,8,12,0.0)');
      g.addColorStop(1, rgba(pal.acc, 0.1));
      ctx.fillStyle = g;
      ctx.fill();
      ctx.strokeStyle = rgba(pal.acc, 0.22);
      ctx.lineWidth = 1;
      ctx.stroke();

      drawGraticule();
      strokeRings(landRings, rgba(pal.acc, 0.16), 1);

      // satellite fleet
      var i;
      for (i = 0; i < SATS.length; i++) drawSatGlyph(satPos(i, satT), i === 0);

      // comet fly-by (over the scene) + the Moon's near half (in front of globe)
      drawComet();
      if (moon.z >= 0) paintMoon(moon);

      if (!reduced) raf = requestAnimationFrame(frame);
    }

    // -- minimal TopoJSON decode --
    // Only the fields this decoder actually reads: topology.arcs (delta-encoded
    // integer pairs), topology.transform.{scale,translate}, and per geometry
    // its type (Polygon / MultiPolygon) and arcs. The payload may come from a
    // CDN, so it is untrusted input; anything else is ignored.
    function decodeArc(topology, i) {
      var arcRaw = topology.arcs[i];
      var sx = topology.transform.scale[0];
      var sy = topology.transform.scale[1];
      var tx = topology.transform.translate[0];
      var ty = topology.transform.translate[1];
      var x = 0;
      var y = 0;
      var out = [];
      for (var k = 0; k < arcRaw.length; k++) {
        var d = arcRaw[k];
        x += d[0];
        y += d[1];
        out.push([x * sx + tx, y * sy + ty]);
      }
      return out;
    }
    function ringCoords(topology, ring) {
      var pts = [];
      for (var i = 0; i < ring.length; i++) {
        var idx = ring[i];
        var arcPts = idx < 0 ? decodeArc(topology, ~idx).slice().reverse() : decodeArc(topology, idx);
        if (i > 0) arcPts = arcPts.slice(1);
        pts = pts.concat(arcPts);
      }
      return pts;
    }
    function geomRings(topology, geom) {
      var out = [];
      var i, j;
      if (geom.type === 'Polygon') {
        for (i = 0; i < geom.arcs.length; i++) out.push(ringCoords(topology, geom.arcs[i]));
        return out;
      }
      if (geom.type === 'MultiPolygon') {
        for (i = 0; i < geom.arcs.length; i++) {
          var poly = geom.arcs[i];
          for (j = 0; j < poly.length; j++) out.push(ringCoords(topology, poly[j]));
        }
        return out;
      }
      return out;
    }

    // Coastlines come from the vendored world-atlas countries-110m (~105 KB,
    // same origin only). When it is missing the graticule globe still renders:
    // the floor, not an error state.
    async function loadEarth() {
      async function topoJson() {
        var urls = ['/login/assets/countries-110m.json'];
        for (var u = 0; u < urls.length; u++) {
          try {
            var r = await fetch(urls[u]);
            if (r.ok) return await r.json();
          } catch (e) {
            /* try the next source */
          }
        }
        return null;
      }
      try {
        var topo = await topoJson();
        if (!topo) return;
        if (!alive) return;
        var geoms =
          (topo.objects && topo.objects.countries && topo.objects.countries.geometries) || [];
        for (var gi = 0; gi < geoms.length; gi++) {
          var gm = geoms[gi];
          var rings = geomRings(topo, gm);
          for (var k = 0; k < rings.length; k++) landRings.push(rings[k]);
        }
        // reduced-motion renders a still frame at mount, BEFORE this fetch
        // resolves - repaint once so those users get coastlines too
        if (reduced) requestAnimationFrame(frame);
      } catch (e) {
        /* offline / blocked - the graticule globe still renders */
      }
    }

    function onResize() {
      build();
    }
    function onTheme() {
      pal = readPalette();
      // reduced-motion shows a still frame; repaint it in the new colours
      if (reduced && alive) requestAnimationFrame(frame);
    }

    build();
    loadEarth();
    raf = requestAnimationFrame(frame);
    if (reduced) requestAnimationFrame(frame);
    window.addEventListener('resize', onResize);
    window.addEventListener('alpaca:themechange', onTheme);

    return function stop() {
      alive = false;
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('alpaca:themechange', onTheme);
    };
  }

  window.AlpacaGlobe = { mount: mount };
})();

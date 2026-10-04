(function () {
  "use strict";
  // Animierte 3D-Szenen statt Fotos auf der Startseite (Canvas, ohne Bibliothek, CSP-konform).
  // Verwendung: <canvas class="scene" data-scene="sphere|rings|helix|wave|orbit|lattice">
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var TAU = Math.PI * 2;

  function seeded(seed) {
    return function () { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  }

  // Jede Szene liefert Punkte [x, y, z] (Einheitswürfel um 0), Kanten [i, j] und optional update(t).
  var SCENES = {
    // Kugel aus Punkten, jeder verbunden mit seinen nächsten Nachbarn
    sphere: function () {
      var n = 46, pts = [], edges = [], hot = {}, i, j, golden = Math.PI * (3 - Math.sqrt(5));
      for (i = 0; i < n; i++) {
        var y = 1 - (i / (n - 1)) * 2, r = Math.sqrt(1 - y * y), th = golden * i;
        pts.push([Math.cos(th) * r, y, Math.sin(th) * r]);
        if (i % 9 === 0) hot[i] = 1;
      }
      for (i = 0; i < n; i++) {
        var near = [];
        for (j = 0; j < n; j++) {
          if (i === j) continue;
          var d = Math.pow(pts[i][0] - pts[j][0], 2) + Math.pow(pts[i][1] - pts[j][1], 2) + Math.pow(pts[i][2] - pts[j][2], 2);
          near.push([d, j]);
        }
        near.sort(function (a, b) { return a[0] - b[0]; });
        for (j = 0; j < 2; j++) if (i < near[j][1]) edges.push([i, near[j][1]]);
      }
      return { pts: pts, edges: edges, hot: hot, spin: 0.45, tilt: -0.3, scale: 0.78 };
    },
    // Drei verschränkte Ringe mit kreisenden Punkten (Gyroskop)
    rings: function () {
      var pts = [], edges = [], hot = {}, k, i, per = 28;
      for (k = 0; k < 3; k++) {
        var base = pts.length;
        for (i = 0; i < per; i++) {
          var a = (i / per) * TAU, r = 1 - k * 0.2, c = Math.cos(a) * r, s = Math.sin(a) * r;
          pts.push(k === 0 ? [c, s, 0] : k === 1 ? [c, 0, s] : [0, c, s]);
          edges.push([base + i, base + (i + 1) % per]);
          if (i % 7 === 0) hot[base + i] = 1;
        }
      }
      return { pts: pts, edges: edges, hot: hot, spin: 0.35, tilt: -0.5, scale: 0.8,
        update: function (t) {
          for (var q = 0; q < 3; q++) for (var m = 0; m < per; m++) {
            var a = (m / per) * TAU + t * (0.5 + q * 0.25) * (q % 2 ? -1 : 1), r = 1 - q * 0.2;
            var c = Math.cos(a) * r, s = Math.sin(a) * r, p = pts[q * per + m];
            if (q === 0) { p[0] = c; p[1] = s; } else if (q === 1) { p[0] = c; p[2] = s; } else { p[1] = c; p[2] = s; }
          }
        } };
    },
    // Doppelhelix mit Sprossen
    helix: function () {
      var pts = [], edges = [], hot = {}, n = 24, i;
      for (i = 0; i < n; i++) {
        var a = i * 0.55, y = (i / (n - 1)) * 2 - 1;
        pts.push([Math.cos(a) * 0.6, y, Math.sin(a) * 0.6]);
        pts.push([Math.cos(a + Math.PI) * 0.6, y, Math.sin(a + Math.PI) * 0.6]);
        edges.push([2 * i, 2 * i + 1]);
        if (i) { edges.push([2 * i - 2, 2 * i]); edges.push([2 * i - 1, 2 * i + 1]); }
        if (i % 5 === 0) hot[2 * i] = 1;
      }
      return { pts: pts, edges: edges, hot: hot, spin: 0.5, tilt: -0.15, scale: 0.75 };
    },
    // Wellenfläche aus Gitterpunkten
    wave: function () {
      var pts = [], edges = [], hot = {}, N = 13, i, j;
      for (i = 0; i < N; i++) for (j = 0; j < N; j++) {
        pts.push([(i / (N - 1)) * 2 - 1, 0, (j / (N - 1)) * 2 - 1]);
        if (j) edges.push([i * N + j - 1, i * N + j]);
        if (i) edges.push([(i - 1) * N + j, i * N + j]);
        if ((i + j) % 6 === 0) hot[i * N + j] = 1;
      }
      return { pts: pts, edges: edges, hot: hot, spin: 0.12, tilt: -0.55, scale: 0.6,
        update: function (t) {
          for (var q = 0; q < pts.length; q++) {
            var p = pts[q];
            p[1] = Math.sin(p[0] * 3 + t * 1.2) * 0.22 + Math.cos(p[2] * 3 - t) * 0.22;
          }
        } };
    },
    // Zentrum mit kreisenden Knoten auf geneigten Bahnen (Menschen um ein gemeinsames Ziel)
    orbit: function () {
      var rnd = seeded(11), pts = [[0, 0, 0]], edges = [], hot = { 0: 1 }, orbs = [], i;
      for (i = 0; i < 16; i++) {
        orbs.push({ r: 0.45 + rnd() * 0.55, a: rnd() * TAU, sp: (0.25 + rnd() * 0.5) * (rnd() > 0.5 ? 1 : -1),
          inc: (rnd() - 0.5) * 1.6 });
        pts.push([0, 0, 0]);
        edges.push([0, i + 1]);
        if (i % 4 === 0) hot[i + 1] = 1;
      }
      return { pts: pts, edges: edges, hot: hot, spin: 0.2, tilt: -0.4, scale: 0.85,
        update: function (t) {
          orbs.forEach(function (o, k) {
            var a = o.a + t * o.sp, x = Math.cos(a) * o.r, z = Math.sin(a) * o.r;
            pts[k + 1][0] = x; pts[k + 1][1] = z * Math.sin(o.inc); pts[k + 1][2] = z * Math.cos(o.inc);
          });
        } };
    },
    // Würfelgitter 3 x 3 x 3
    lattice: function () {
      var pts = [], edges = [], hot = {}, i, j, k, N = 3, id = function (a, b, c) { return (a * N + b) * N + c; };
      for (i = 0; i < N; i++) for (j = 0; j < N; j++) for (k = 0; k < N; k++) {
        pts.push([i - 1, j - 1, k - 1]);
        if (k) edges.push([id(i, j, k - 1), id(i, j, k)]);
        if (j) edges.push([id(i, j - 1, k), id(i, j, k)]);
        if (i) edges.push([id(i - 1, j, k), id(i, j, k)]);
        if ((i + j + k) % 3 === 0) hot[id(i, j, k)] = 1;
      }
      pts.forEach(function (p) { p[0] *= 0.55; p[1] *= 0.55; p[2] *= 0.55; });
      return { pts: pts, edges: edges, hot: hot, spin: 0.4, tilt: -0.45, scale: 0.9,
        update: function (t) {
          var s = 1 + Math.sin(t * 0.8) * 0.12;
          for (var q = 0; q < pts.length; q++) {
            var b = pts[q];
            if (!b.o) b.o = [b[0], b[1], b[2]];
            b[0] = b.o[0] * s; b[1] = b.o[1] * s; b[2] = b.o[2] * s;
          }
        } };
    }
  };

  function init(canvas) {
    var make = SCENES[canvas.getAttribute("data-scene")] || SCENES.sphere;
    var sc = make(), ctx = canvas.getContext("2d");
    var css = getComputedStyle(document.documentElement);
    var ember = (css.getPropertyValue("--ember") || "#14b8a6").trim();
    var bone = (css.getPropertyValue("--bone") || "#eef2f6").trim();
    var ash = (css.getPropertyValue("--ash") || "#9ba5b1").trim();
    var w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    var t = 0, ang = 0.6 + (canvas.getAttribute("data-phase") | 0) * 1.3, running = false, visible = true, proj = [];
    var tx = 0, ty = 0;

    function resize() {
      var r = canvas.getBoundingClientRect();
      w = Math.max(120, r.width); h = Math.max(120, r.height);
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function project() {
      var ca = Math.cos(ang + tx), sa = Math.sin(ang + tx), ct = Math.cos(sc.tilt + ty), st = Math.sin(sc.tilt + ty);
      var R = Math.min(w, h) * 0.5 * sc.scale;
      proj = sc.pts.map(function (p) {
        var x = p[0] * ca + p[2] * sa, z = -p[0] * sa + p[2] * ca;
        var y = p[1] * ct - z * st; z = p[1] * st + z * ct;
        var s = 1 / (1 - z * 0.3);
        return { x: w / 2 + x * R * s, y: h / 2 + y * R * s, s: s, z: z };
      });
    }

    function draw() {
      if (sc.update) sc.update(t);
      project();
      ctx.clearRect(0, 0, w, h);
      sc.edges.forEach(function (e) {
        var a = proj[e[0]], b = proj[e[1]], hot = sc.hot[e[0]] && sc.hot[e[1]];
        ctx.globalAlpha = Math.min(0.85, 0.1 + 0.32 * (a.s + b.s) / 2) * (hot ? 1.5 : 1);
        ctx.strokeStyle = hot ? ember : ash;
        ctx.lineWidth = hot ? 1.5 : 1;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      });
      proj.map(function (p, i) { return [p, i]; }).sort(function (a, b) { return a[0].z - b[0].z; }).forEach(function (q) {
        var p = q[0], isHot = sc.hot[q[1]], r = (isHot ? 4.6 : 2.8) * p.s;
        ctx.globalAlpha = 0.35 + 0.65 * Math.min(1, p.s * 0.8);
        ctx.fillStyle = isHot ? ember : bone;
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, TAU); ctx.fill();
        if (isHot) { ctx.globalAlpha = 0.16; ctx.beginPath(); ctx.arc(p.x, p.y, r * 2.6, 0, TAU); ctx.fill(); }
      });
      ctx.globalAlpha = 1;
    }

    function tick() {
      if (!running) return;
      t += 0.016; ang += 0.0035 * (sc.spin || 0.4) * 2;
      draw();
      requestAnimationFrame(tick);
    }
    function start() { if (!running && !reduce && visible) { running = true; requestAnimationFrame(tick); } }
    function stop() { running = false; }

    canvas.addEventListener("pointermove", function (ev) {
      var r = canvas.getBoundingClientRect();
      tx = ((ev.clientX - r.left) / w - 0.5) * 0.7; ty = ((ev.clientY - r.top) / h - 0.5) * 0.4;
      if (!running) draw();
    });
    canvas.addEventListener("pointerleave", function () { tx = ty = 0; if (!running) draw(); });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (es) {
        visible = es[0].isIntersecting;
        if (visible) start(); else stop();
      }).observe(canvas);
    }
    window.addEventListener("resize", resize);
    resize();
    start();
  }

  document.querySelectorAll("canvas[data-scene]").forEach(init);
})();

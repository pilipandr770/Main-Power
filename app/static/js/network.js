(function () {
  "use strict";
  // Rotierendes 3D-Netz aus Menschen und Verbindungen (Canvas, ohne Bibliothek, CSP-konform).
  // Daten: <canvas data-network data-graph='{"nodes":[{"l":"Julia","u":"/app/mitglieder/3","h":1}],"edges":[[0,1,0.8]]}'>
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function seeded(seed) {
    return function () { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  }

  function demoGraph() {
    var rnd = seeded(42), nodes = [], edges = [], n = 34, i, j;
    for (i = 0; i < n; i++) nodes.push({ l: "", h: i % 7 === 0 ? 1 : 0 });
    for (i = 0; i < n; i++) {
      var links = 1 + Math.floor(rnd() * 2);
      for (j = 0; j < links; j++) edges.push([i, Math.floor(rnd() * n), 0.3 + rnd() * 0.7]);
    }
    return { nodes: nodes, edges: edges };
  }

  function init(canvas) {
    var graph;
    try { graph = JSON.parse(canvas.getAttribute("data-graph") || "null"); } catch (e) { graph = null; }
    if (!graph || !graph.nodes || !graph.nodes.length) graph = demoGraph();
    var ctx = canvas.getContext("2d");
    var css = getComputedStyle(document.documentElement);
    var ember = (css.getPropertyValue("--ember") || "#14b8a6").trim();
    var bone = (css.getPropertyValue("--bone") || "#eef2f6").trim();
    var ash = (css.getPropertyValue("--ash") || "#aaa19b").trim();
    var w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
    var rnd = seeded(7), pts = [];
    var golden = Math.PI * (3 - Math.sqrt(5));
    var n = graph.nodes.length;
    // Fibonacci-Kugel: gleichmäßig verteilte Punkte, leicht "zerknittert"
    graph.nodes.forEach(function (nd, i) {
      var y = n > 1 ? 1 - (i / (n - 1)) * 2 : 0, r = Math.sqrt(1 - y * y), th = golden * i;
      var k = 0.85 + rnd() * 0.3;
      pts.push({ x: Math.cos(th) * r * k, y: y * k, z: Math.sin(th) * r * k, n: nd, sx: 0, sy: 0, s: 1 });
    });
    var ang = 0.6, tilt = -0.25, tx = 0, ty = 0, hover = -1, running = false, visible = true;

    function resize() {
      var r = canvas.getBoundingClientRect();
      w = Math.max(200, r.width); h = Math.max(160, r.height);
      canvas.width = w * dpr; canvas.height = h * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function project() {
      var ca = Math.cos(ang + tx), sa = Math.sin(ang + tx), ct = Math.cos(tilt + ty), st = Math.sin(tilt + ty);
      var R = Math.min(w, h) * 0.42;
      pts.forEach(function (p) {
        var x = p.x * ca + p.z * sa, z = -p.x * sa + p.z * ca;
        var y = p.y * ct - z * st; z = p.y * st + z * ct;
        var s = 1 / (1 - z * 0.35);
        p.sx = w / 2 + x * R * s * 1.25; p.sy = h / 2 + y * R * s; p.s = s; p.z2 = z;
      });
    }

    function draw() {
      project();
      ctx.clearRect(0, 0, w, h);
      graph.edges.forEach(function (e) {
        var a = pts[e[0]], b = pts[e[1]];
        if (!a || !b || a === b) return;
        var depth = (a.s + b.s) / 2, hot = a.n.h && b.n.h;
        ctx.globalAlpha = Math.min(0.9, 0.12 + (e[2] || 0.5) * 0.35 * depth) * (hot ? 1.6 : 1);
        ctx.strokeStyle = hot || (e[2] || 0) > 0.75 ? ember : ash;
        ctx.lineWidth = hot ? 1.6 : 1;
        ctx.beginPath(); ctx.moveTo(a.sx, a.sy); ctx.lineTo(b.sx, b.sy); ctx.stroke();
      });
      pts.slice().sort(function (a, b) { return a.z2 - b.z2; }).forEach(function (p) {
        var r = (p.n.h ? 5.2 : 3.4) * p.s, idx = pts.indexOf(p);
        ctx.globalAlpha = 0.35 + 0.65 * Math.min(1, p.s * 0.8);
        ctx.fillStyle = p.n.h ? ember : bone;
        ctx.beginPath(); ctx.arc(p.sx, p.sy, idx === hover ? r * 1.6 : r, 0, 6.2832); ctx.fill();
        if (p.n.h) { ctx.globalAlpha = 0.18; ctx.beginPath(); ctx.arc(p.sx, p.sy, r * 2.6, 0, 6.2832); ctx.fill(); }
      });
      ctx.globalAlpha = 1;
      if (hover >= 0 && pts[hover].n.l) {
        var q = pts[hover];
        ctx.font = "600 13px system-ui, sans-serif";
        var tw = ctx.measureText(q.n.l).width;
        ctx.fillStyle = "rgba(12,10,9,.9)"; ctx.fillRect(q.sx + 10, q.sy - 22, tw + 14, 24);
        ctx.fillStyle = bone; ctx.fillText(q.n.l, q.sx + 17, q.sy - 5);
      }
    }

    function tick() {
      if (!running) return;
      ang += 0.0035;
      draw();
      requestAnimationFrame(tick);
    }
    function start() { if (!running && !reduce && visible) { running = true; requestAnimationFrame(tick); } }
    function stop() { running = false; }

    canvas.addEventListener("pointermove", function (ev) {
      var r = canvas.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
      tx = ((mx / w) - 0.5) * 0.6; ty = ((my / h) - 0.5) * 0.4;
      var best = -1, bd = 16 * 16;
      pts.forEach(function (p, i) {
        var d = (p.sx - mx) * (p.sx - mx) + (p.sy - my) * (p.sy - my);
        if (d < bd && p.n.l) { bd = d; best = i; }
      });
      hover = best;
      canvas.style.cursor = best >= 0 && pts[best].n.u ? "pointer" : "default";
      if (!running) draw();
    });
    canvas.addEventListener("pointerleave", function () { hover = -1; tx = ty = 0; if (!running) draw(); });
    canvas.addEventListener("click", function () {
      if (hover >= 0 && pts[hover].n.u) window.location.href = pts[hover].n.u;
    });
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

  document.querySelectorAll("canvas[data-network]").forEach(init);
})();

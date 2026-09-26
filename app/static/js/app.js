(function () {
  "use strict";
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  // Mobile navigation
  document.querySelectorAll("[data-rail-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var rail = document.querySelector(".rail");
      if (!rail) return;
      var open = rail.classList.toggle("open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });

  // Confirmation for destructive forms
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) {
      if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Minimal, safe rendering of assistant text
  function render(text) {
    var esc = String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    esc = esc.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    esc = esc.replace(/(^|\s)_(\S[^_]*\S)_(?=\s|$|[.,!?)])/g, "$1<em>$2</em>");
    esc = esc.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, '<a href="$1" rel="noopener" target="_blank">$1</a>');
    return esc;
  }

  function initChat(root) {
    var log = root.querySelector(".chat-log");
    var form = root.querySelector(".chat-form");
    var input = form.querySelector("textarea");
    var button = form.querySelector("button");
    var endpoint = root.getAttribute("data-endpoint");
    var mode = root.getAttribute("data-mode");
    var storeKey = "mp-aiko-public";
    var history = [];

    if (mode === "public") {
      try { history = JSON.parse(sessionStorage.getItem(storeKey) || "[]"); } catch (e) { history = []; }
      history.forEach(function (m) { add(m.role, m.content); });
    }
    log.querySelectorAll(".msg.assistant[data-raw]").forEach(function (el) {
      el.innerHTML = render(el.getAttribute("data-raw"));
    });
    log.scrollTop = log.scrollHeight;

    function add(role, content, pending) {
      var el = document.createElement("div");
      el.className = "msg " + role + (pending ? " pending" : "");
      if (role === "assistant") el.innerHTML = render(content); else el.textContent = content;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
      return el;
    }

    function send(text) {
      text = (text || "").trim();
      if (!text) return;
      add("user", text);
      input.value = "";
      button.disabled = true;
      var wait = add("assistant", "Aiko denkt nach …", true);
      var body;
      if (mode === "public") {
        history.push({ role: "user", content: text });
        body = { history: history.slice(-10) };
      } else {
        body = { message: text };
      }
      fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "Accept": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, d: d }; });
      }).then(function (res) {
        var reply = res.ok ? res.d.reply : (res.d.error || "Das hat nicht geklappt. Versuch es gleich noch einmal.");
        wait.classList.remove("pending");
        wait.innerHTML = render(reply);
        if (mode === "public" && res.ok) {
          history.push({ role: "assistant", content: reply });
          try { sessionStorage.setItem(storeKey, JSON.stringify(history.slice(-20))); } catch (e) {}
        }
      }).catch(function () {
        wait.classList.remove("pending");
        wait.textContent = "Keine Verbindung. Prüf dein Internet und versuch es erneut.";
      }).finally(function () {
        button.disabled = false;
        input.focus();
        log.scrollTop = log.scrollHeight;
      });
    }

    form.addEventListener("submit", function (e) { e.preventDefault(); send(input.value); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); }
    });
    root.querySelectorAll("[data-suggest]").forEach(function (b) {
      b.addEventListener("click", function () { send(b.getAttribute("data-suggest")); });
    });
  }
  document.querySelectorAll("[data-chat]").forEach(initChat);

  // Public floating widget
  var fab = document.querySelector("[data-aiko-open]");
  var panel = document.querySelector(".aiko-panel");
  if (fab && panel) {
    fab.addEventListener("click", function () {
      panel.hidden = !panel.hidden;
      fab.setAttribute("aria-expanded", panel.hidden ? "false" : "true");
      if (!panel.hidden) { var t = panel.querySelector("textarea"); if (t) t.focus(); }
    });
    document.querySelectorAll("[data-aiko-close]").forEach(function (b) {
      b.addEventListener("click", function () { panel.hidden = true; fab.setAttribute("aria-expanded", "false"); });
    });
  }

  // ---------- KI-Hilfen ----------
  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf, "Accept": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // Beispielantworten per Klick in das Feld übernehmen
  document.querySelectorAll("[data-example]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = document.getElementById(b.getAttribute("data-target"));
      if (!t) return;
      t.value = b.getAttribute("data-example");
      t.focus();
    });
  });

  // Profil-Coach: Feedback zu den Antworten (auch ungespeichert)
  document.querySelectorAll("[data-coach]").forEach(function (box) {
    var out = box.querySelector("[data-coach-out]");
    var btn = box.querySelector("[data-coach-run]");
    btn.addEventListener("click", function () {
      var body = {};
      ["q_focus", "q_challenge", "q_can_help", "q_looking_for", "headline"].forEach(function (k) {
        var f = document.getElementById("f-" + k);
        if (f) body[k] = f.value;
      });
      btn.disabled = true;
      out.innerHTML = "";
      out.appendChild(el("span", "skeleton"));
      out.appendChild(el("span", "skeleton"));
      postJSON(box.getAttribute("data-endpoint"), body).then(function (res) {
        out.innerHTML = "";
        if (!res.ok) { out.textContent = res.d.error || "Gerade nicht möglich. Versuch es gleich noch einmal."; return; }
        out.appendChild(el("p", "mb-0", res.d.summary || ""));
        if (res.d.tips && res.d.tips.length) {
          var ul = el("ul");
          res.d.tips.forEach(function (t) {
            var li = el("li");
            var lbl = document.querySelector('label[for="f-' + t.field + '"]');
            li.appendChild(el("b", null, (lbl ? lbl.childNodes[0].textContent.trim() : t.field) + ": "));
            li.appendChild(document.createTextNode(t.tip));
            ul.appendChild(li);
          });
          out.appendChild(ul);
        }
        if (res.d.ai === false) out.appendChild(el("p", "small muted mb-0", "Einfache Prüfung ohne KI-Anbindung."));
      }).catch(function () {
        out.textContent = "Keine Verbindung. Versuch es gleich noch einmal.";
      }).finally(function () { btn.disabled = false; });
    });
  });

  // Kontakt-Assistent auf der Mitgliederseite
  document.querySelectorAll("[data-insight]").forEach(function (box) {
    var body = box.querySelector("[data-insight-body]");
    var refresh = box.querySelector("[data-insight-refresh]");
    var endpoint = box.getAttribute("data-endpoint");
    var target = document.getElementById(box.getAttribute("data-target"));

    function block(title, text) {
      var d = el("div", "block");
      d.appendChild(el("h4", null, title));
      d.appendChild(el("p", "mb-0", text));
      return d;
    }
    function show(d) {
      body.innerHTML = "";
      if (d.they_help_you) body.appendChild(block("Was dir der Kontakt bringen kann", d.they_help_you));
      if (d.you_help_them) body.appendChild(block("Was du beitragen kannst", d.you_help_them));
      if (d.message_draft) {
        var wrap = el("div", "block");
        wrap.appendChild(el("h4", null, "Nachrichtenentwurf"));
        var ta = el("textarea", "draft");
        ta.value = d.message_draft;
        ta.setAttribute("aria-label", "Nachrichtenentwurf");
        wrap.appendChild(ta);
        var row = el("div", "row mt-s");
        if (target) {
          var use = el("button", "btn btn-sm", "In meine Anfrage übernehmen");
          use.type = "button";
          use.addEventListener("click", function () {
            target.value = ta.value.slice(0, 1000);
            target.scrollIntoView({ behavior: "smooth", block: "center" });
            target.focus();
          });
          row.appendChild(use);
        }
        var copy = el("button", "btn btn-ghost btn-sm", "Kopieren");
        copy.type = "button";
        copy.addEventListener("click", function () {
          if (navigator.clipboard) navigator.clipboard.writeText(ta.value);
          copy.textContent = "Kopiert";
        });
        row.appendChild(copy);
        wrap.appendChild(row);
        body.appendChild(wrap);
      }
      if (d.event) {
        var eb = el("div", "event-box");
        eb.appendChild(el("h4", null, "Hier könnt ihr euch treffen"));
        var a = el("a", null, d.event.title);
        a.href = d.event.url;
        var line = el("p", "mb-0");
        var strong = el("b");
        strong.appendChild(a);
        line.appendChild(strong);
        line.appendChild(document.createTextNode(" · " + d.event.when));
        eb.appendChild(line);
        if (d.event.reason) eb.appendChild(el("p", "small muted mb-0 mt-s", d.event.reason));
        body.appendChild(eb);
      }
      if (d.ai === false) body.appendChild(el("p", "small muted mb-0 mt-s", "Vorlage ohne KI-Anbindung."));
      refresh.hidden = false;
    }
    function load(force) {
      body.innerHTML = "";
      ["", "", ""].forEach(function () { body.appendChild(el("span", "skeleton")); });
      refresh.hidden = true;
      postJSON(endpoint, { refresh: !!force }).then(function (res) {
        if (!res.ok) { body.innerHTML = ""; body.appendChild(el("p", "muted mb-0", res.d.error || "Gerade nicht möglich.")); return; }
        show(res.d);
      }).catch(function () {
        body.innerHTML = "";
        body.appendChild(el("p", "muted mb-0", "Keine Verbindung. Lade die Seite neu, um es erneut zu versuchen."));
      });
    }
    refresh.addEventListener("click", function () { load(true); });
    load(false);
  });

  // ---------- Cookie-Hinweis (nur notwendige Cookies: reiner Informationshinweis, Merker im lokalen Speicher) ----------
  var CK = "mp-cookie-hinweis";
  var banner = document.querySelector("[data-cookie-banner]");
  function ckGet() { try { return localStorage.getItem(CK); } catch (e) { return "1"; } }
  if (banner && !ckGet()) banner.hidden = false;
  var ok = document.querySelector("[data-cookie-ok]");
  if (ok) ok.addEventListener("click", function () {
    try { localStorage.setItem(CK, "1"); } catch (e) {}
    banner.hidden = true;
  });
  document.querySelectorAll("[data-cookie-reset]").forEach(function (b) {
    b.addEventListener("click", function () {
      try { localStorage.removeItem(CK); } catch (e) {}
      if (banner) banner.hidden = false;
    });
  });

  // Demo-Modus: Nutzerwechsel per Auswahlfeld
  document.querySelectorAll("[data-switch-select]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      var f = sel.closest("form");
      f.setAttribute("action", sel.value);
      f.submit();
    });
  });

  // Formulare mit langer Laufzeit: Button sperren und Hinweistext zeigen
  document.querySelectorAll("form[data-busy]").forEach(function (f) {
    f.addEventListener("submit", function () {
      var b = f.querySelector("button[type=submit]");
      if (b) { b.disabled = true; b.textContent = f.getAttribute("data-busy"); }
    });
  });
  document.querySelectorAll("[data-print]").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });

  // Balken (Breite per CSSOM, CSP-konform)
  document.querySelectorAll("[data-w]").forEach(function (el) {
    var w = parseFloat(el.getAttribute("data-w")) || 0;
    el.style.width = Math.max(0, Math.min(100, w)) + "%";
  });

  // Markt-Panel: Fortschritt abfragen und bei Abschluss neu laden
  document.querySelectorAll("[data-panel-progress]").forEach(function (box) {
    var url = box.getAttribute("data-endpoint");
    var bar = box.querySelector("[data-panel-bar]");
    var label = box.querySelector("[data-panel-label]");
    function tick() {
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var pct = d.requested ? Math.round(100 * d.done / d.requested) : 0;
          bar.style.width = pct + "%";
          label.textContent = d.done + " von " + d.requested + " Personas haben geantwortet" + (d.status === "running" && d.done >= d.requested ? " — Aiko wertet aus …" : "");
          if (d.status === "done" || d.status === "failed") { window.location.reload(); return; }
          setTimeout(tick, 2000);
        })
        .catch(function () { setTimeout(tick, 4000); });
    }
    tick();
  });
})();

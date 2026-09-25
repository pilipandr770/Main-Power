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
})();

/* DEL assistant page: scope/target picker, prompt cards, NDJSON streaming
   transcript. Page-only script; registers window.DEL.assistant = { init }. */
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Safe Markdown subset. Everything is HTML-escaped first; the renderer
  // then only ever emits its own tags. Links: same-origin "/..." paths only.
  // ---------------------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function inline(text) {
    return text
      .replace(/`([^`\n]+)`/g, function (_, c) { return "<code>" + c + "</code>"; })
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((\/(?!\/)[^)\s]*)\)/g, '<a href="$2">$1</a>');
  }

  function renderMarkdown(src) {
    var lines = escapeHtml(src || "").replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var list = null; // "ul" | "ol"
    var para = [];
    var code = null;

    function flushPara() {
      if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
    }
    function closeList() {
      if (list) { out.push("</" + list + ">"); list = null; }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (code !== null) {
        if (/^```/.test(line)) { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = null; }
        else code.push(line);
        continue;
      }
      if (/^```/.test(line)) { flushPara(); closeList(); code = []; continue; }
      var m;
      if ((m = /^(#{1,3})\s+(.*)$/.exec(line))) {
        flushPara(); closeList();
        var lvl = m[1].length + 2; // h3..h5 keep the page's own h1/h2 hierarchy
        out.push("<h" + lvl + ">" + inline(m[2]) + "</h" + lvl + ">");
        continue;
      }
      if ((m = /^\s*[-*•]\s+(.*)$/.exec(line))) {
        flushPara();
        if (list !== "ul") { closeList(); list = "ul"; out.push("<ul>"); }
        out.push("<li>" + inline(m[1]) + "</li>");
        continue;
      }
      if ((m = /^\s*\d+[.)]\s+(.*)$/.exec(line))) {
        flushPara();
        if (list !== "ol") { closeList(); list = "ol"; out.push("<ol>"); }
        out.push("<li>" + inline(m[1]) + "</li>");
        continue;
      }
      if (!line.trim()) { flushPara(); closeList(); continue; }
      if (list) closeList();
      para.push(line.trim());
    }
    if (code !== null) out.push("<pre><code>" + code.join("\n") + "</code></pre>");
    flushPara(); closeList();
    return out.join("");
  }

  // ---------------------------------------------------------------------
  // Page controller
  // ---------------------------------------------------------------------
  function init(root) {
    var $ = function (id) { return document.getElementById(id); };
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrf = csrfMeta ? csrfMeta.getAttribute("content") : "";
    var toast = (window.DEL && window.DEL.toast) || function () {};

    var state = {
      scope: root.getAttribute("data-scope") || "general",
      target: root.getAttribute("data-target") || "",
      rtype: root.getAttribute("data-resource-type") || "",
      conversation: root.getAttribute("data-conversation") || null,
      controller: null,
    };

    var chips = root.querySelectorAll(".assistant-scope-chip");
    var targetWrap = $("assistant-target-wrap");
    var targetSel = $("assistant-target");
    var targetLabel = $("assistant-target-label");
    var rtypeWrap = $("assistant-rtype-wrap");
    var rtypeSel = $("assistant-rtype");
    var searchWrap = $("assistant-search-wrap");
    var search = $("assistant-target-search");
    var promptsBox = $("assistant-prompts");
    var transcript = $("assistant-transcript");
    var form = $("assistant-form");
    var input = $("assistant-input");
    if (!form || !input || !transcript) return;
    var sendBtn = $("assistant-send");
    var stopBtn = $("assistant-stop");
    var truncatedBadge = $("assistant-truncated");
    var usageEl = $("assistant-usage");
    var convList = $("assistant-conversations");

    function parseData(attr) {
      try { return JSON.parse(targetSel.getAttribute(attr) || "[]"); } catch (e) { return []; }
    }
    var cache = { app: parseData("data-apps"), resource_type: parseData("data-rtypes"), resource: {} };
    if (state.rtype && cache.resource) cache.resource[state.rtype] = parseData("data-resources");

    // Render saved assistant messages (server put raw text in the bubble).
    transcript.querySelectorAll(".assistant-msg[data-md]").forEach(function (el) {
      el.innerHTML = renderMarkdown(el.textContent);
      el.removeAttribute("data-md");
    });
    transcript.scrollTop = transcript.scrollHeight;

    function fetchJson(url) {
      return fetch(url, { credentials: "same-origin" }).then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
          return data;
        });
      });
    }

    function fillTargets(items, placeholder) {
      var q = (search.value || "").toLowerCase();
      targetSel.innerHTML = "";
      var first = document.createElement("option");
      first.value = "";
      first.textContent = placeholder;
      targetSel.appendChild(first);
      items.forEach(function (t) {
        if (q && (t.label + " " + t.value).toLowerCase().indexOf(q) === -1) return;
        var opt = document.createElement("option");
        opt.value = t.value;
        opt.textContent = t.label;
        targetSel.appendChild(opt);
      });
      targetSel.value = state.target;
      if (targetSel.value !== state.target) { targetSel.value = ""; }
    }

    function loadTargets() {
      var scope = state.scope;
      if (scope === "general" || scope === "orphans") {
        targetWrap.hidden = true;
        return Promise.resolve();
      }
      targetWrap.hidden = false;
      rtypeWrap.hidden = scope !== "resource";
      searchWrap.hidden = scope !== "resource";
      targetLabel.textContent = scope === "app" ? "Application" : scope === "resource_type" ? "Resource type" : "Resource";
      var placeholder = scope === "app" ? "Choose an application…" : scope === "resource_type" ? "Choose a type…" : "Choose a resource…";
      if (scope === "app" || scope === "resource_type") {
        if (cache[scope].length) { fillTargets(cache[scope], placeholder); return Promise.resolve(); }
        return fetchJson("/assistant/targets?scope=" + scope).then(function (d) {
          cache[scope] = d.targets || [];
          fillTargets(cache[scope], placeholder);
        });
      }
      // resource: type select first, then that type's resources
      var rt = rtypeSel.value || state.rtype || (cache.resource_type[0] && cache.resource_type[0].value) || "container";
      rtypeSel.value = rt;
      state.rtype = rt;
      if (cache.resource[rt]) { fillTargets(cache.resource[rt], placeholder); return Promise.resolve(); }
      return fetchJson("/assistant/targets?scope=resource&type=" + encodeURIComponent(rt)).then(function (d) {
        cache.resource[rt] = d.targets || [];
        fillTargets(cache.resource[rt], placeholder);
      });
    }

    function loadPrompts() {
      if (!promptsBox) return Promise.resolve();
      var url = "/assistant/prompts?scope=" + encodeURIComponent(state.scope) +
        (state.target ? "&target=" + encodeURIComponent(state.target) : "");
      return fetchJson(url).then(function (d) {
        promptsBox.innerHTML = "";
        var items = d.prompts || [];
        if (!items.length) {
          var none = document.createElement("div");
          none.className = "muted text-sm";
          none.textContent = "No suggestions for this scope.";
          promptsBox.appendChild(none);
          return;
        }
        items.forEach(function (p) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "assistant-prompt";
          b.setAttribute("data-prompt-id", p.id);
          b.setAttribute("data-text", p.text);
          b.title = p.description || p.text;
          var lab = document.createElement("span");
          lab.className = "assistant-prompt-label";
          lab.textContent = p.label || p.id;
          b.appendChild(lab);
          if (p.description) {
            var desc = document.createElement("span");
            desc.className = "assistant-prompt-desc";
            desc.textContent = p.description;
            b.appendChild(desc);
          }
          promptsBox.appendChild(b);
        });
      }).catch(function () {});
    }

    function updatePlaceholder() {
      if (!input) return;
      var names = {
        general: "the inventory",
        app: "this app",
        orphans: "orphans",
        resource_type: "this type",
        resource: "this resource",
      };
      var named = "";
      if (state.target && targetSel && targetSel.value === state.target) {
        var opt = targetSel.options[targetSel.selectedIndex];
        named = (opt && opt.textContent) ? opt.textContent.trim() : "";
      }
      if (!named && state.target) named = state.target;
      input.placeholder = named
        ? ("Ask about " + named + "…")
        : ("Ask about " + (names[state.scope] || "this screen") + "…");
    }

    function syncChips() {
      chips.forEach(function (c) {
        c.setAttribute("aria-pressed", c.getAttribute("data-scope") === state.scope ? "true" : "false");
      });
      root.setAttribute("data-scope", state.scope);
      root.setAttribute("data-target", state.target || "");
      if (state.rtype) root.setAttribute("data-resource-type", state.rtype);
    }

    function setScope(scope) {
      if (scope === state.scope) return;
      state.scope = scope;
      state.target = "";
      if (search) search.value = "";
      syncChips();
      newConversation();
      loadTargets().then(updatePlaceholder).catch(function (e) { toast(e.message, "error"); });
      loadPrompts();
    }

    function applyAsk(scope, target, rtype, draft) {
      if (scope) state.scope = scope;
      state.target = target || "";
      if (rtype) {
        state.rtype = rtype;
        if (rtypeSel) rtypeSel.value = rtype;
      }
      if (search) search.value = "";
      syncChips();
      newConversation();
      loadTargets().then(function () {
        if (state.target && targetSel) {
          targetSel.value = state.target;
          if (targetSel.value !== state.target) targetSel.value = "";
          else state.target = targetSel.value;
        }
        updatePlaceholder();
      }).catch(function (e) { toast(e.message, "error"); });
      loadPrompts().then(function () {
        if (draft) fillComposer(draft);
        else input.focus();
      });
    }

    function newConversation() {
      if (state.controller) state.controller.abort();
      state.conversation = null;
      transcript.innerHTML = "";
      truncatedBadge.hidden = true;
      usageEl.textContent = "";
      if (root.id === "assistant-page" && window.history && window.history.replaceState) {
        var qs = "?scope=" + encodeURIComponent(state.scope) +
          (state.target ? "&target=" + encodeURIComponent(state.target) : "");
        window.history.replaceState(null, "", "/assistant" + qs);
      }
    }

    chips.forEach(function (c) {
      c.addEventListener("click", function () { setScope(c.getAttribute("data-scope")); });
    });
    targetSel.addEventListener("change", function () {
      state.target = targetSel.value;
      root.setAttribute("data-target", state.target || "");
      if (state.conversation) newConversation();
      loadPrompts();
      updatePlaceholder();
    });
    rtypeSel.addEventListener("change", function () {
      state.target = "";
      search.value = "";
      if (state.conversation) newConversation();
      loadTargets().catch(function (e) { toast(e.message, "error"); });
    });
    search.addEventListener("input", function () {
      var rt = state.rtype;
      if (cache.resource[rt]) fillTargets(cache.resource[rt], "Choose a resource…");
    });
    function fillComposer(text, promptId) {
      var body = promptTemplate(text || "");
      input.value = body;
      input.setAttribute("data-filled-id", promptId || "");
      input.setAttribute("data-filled-text", body);
      input.focus();
      var slot = body.search(/\[(app|APP|type:key)\]/);
      if (slot >= 0) {
        var end = body.indexOf("]", slot);
        if (end > slot) input.setSelectionRange(slot, end + 1);
      } else {
        input.setSelectionRange(body.length, body.length);
      }
    }
    function promptTemplate(text) {
      if (state.scope === "app" && !state.target && text.indexOf("[app]") === -1) {
        return text.replace(/\?\s*$/, "") + " for [app]?";
      }
      if (state.scope === "resource" && !state.target && text.indexOf("[type:key]") === -1) {
        return text.replace(/\?\s*$/, "") + " Resource: [type:key]";
      }
      return text;
    }
    promptsBox.addEventListener("click", function (evt) {
      var btn = evt.target.closest(".assistant-prompt");
      if (!btn) return;
      fillComposer(btn.getAttribute("data-text"), btn.getAttribute("data-prompt-id"));
    });

    // ----- transcript helpers -----
    function bubble(role, text) {
      var empty = $("assistant-empty");
      if (empty) empty.remove();
      var el = document.createElement("div");
      el.className = "assistant-msg assistant-msg-" + role;
      el.textContent = text;
      transcript.appendChild(el);
      transcript.scrollTop = transcript.scrollHeight;
      return el;
    }

    function addConversationEntry(id, title) {
      if (!convList) return;
      var none = $("assistant-no-conversations");
      if (none) none.remove();
      var li = document.createElement("li");
      li.className = "assistant-conv is-active";
      var a = document.createElement("a");
      a.className = "assistant-conv-link";
      a.href = "/assistant?conversation=" + id;
      var t = document.createElement("span");
      t.className = "assistant-conv-title";
      t.textContent = title.slice(0, 80);
      a.appendChild(t);
      li.appendChild(a);
      convList.querySelectorAll(".is-active").forEach(function (x) { x.classList.remove("is-active"); });
      li.classList.add("is-active");
      convList.insertBefore(li, convList.firstChild);
    }

    function setBusy(busy) {
      sendBtn.disabled = busy;
      sendBtn.classList.toggle("is-busy", busy);
      stopBtn.hidden = !busy;
      input.readOnly = busy;
    }

    // ----- ask: POST + NDJSON stream -----
    function ask(message, promptId) {
      if (state.controller) return;
      message = (message || "").trim();
      if (!message) return;
      if (state.scope !== "general" && state.scope !== "orphans" && !state.target) {
        toast("Choose a target first.", "error");
        targetSel.focus();
        return;
      }
      bubble("user", message);
      var reply = bubble("assistant", "");
      reply.classList.add("is-streaming");
      var text = "";
      var pending = false;
      function paint() {
        pending = false;
        reply.innerHTML = renderMarkdown(text);
        transcript.scrollTop = transcript.scrollHeight;
      }
      function schedulePaint() {
        if (pending) return;
        pending = true;
        window.requestAnimationFrame(paint);
      }
      function fail(msg) {
        reply.classList.remove("is-streaming");
        reply.classList.add("is-error");
        reply.textContent = msg;
        toast(msg, "error");
      }
      function handle(evt) {
        if (!evt || !evt.type) return;
        if (evt.type === "meta") {
          if (evt.conversation_id && !state.conversation) {
            state.conversation = String(evt.conversation_id);
            addConversationEntry(evt.conversation_id, message);
            if (root.id === "assistant-page" && window.history && window.history.replaceState) {
              window.history.replaceState(null, "", "/assistant?conversation=" + evt.conversation_id);
            }
          }
          truncatedBadge.hidden = !evt.context_truncated;
        } else if (evt.type === "delta") {
          text += evt.text || "";
          schedulePaint();
        } else if (evt.type === "done") {
          var u = evt.usage || {};
          var parts = [];
          if (u.prompt_eval_count != null) parts.push(u.prompt_eval_count + " in");
          if (u.eval_count != null) parts.push(u.eval_count + " out");
          if (u.ms != null) parts.push(Math.round(u.ms / 100) / 10 + "s");
          usageEl.textContent = parts.length ? "· " + parts.join(" · ") : "";
        } else if (evt.type === "error") {
          text += (text ? "\n\n" : "") + "**Error:** " + (evt.message || evt.kind || "unknown");
          reply.classList.add("is-error");
          schedulePaint();
        }
      }

      var controller = new AbortController();
      state.controller = controller;
      setBusy(true);
      input.value = "";

      fetch("/assistant/ask", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          scope: state.scope,
          target: state.target || null,
          message: message,
          conversation_id: state.conversation ? Number(state.conversation) : null,
          prompt_id: promptId || null,
        }),
        signal: controller.signal,
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (d) {
            throw new Error(d.error || ("Request failed (" + resp.status + ")"));
          });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = "";
        function pump() {
          return reader.read().then(function (r) {
            buf += decoder.decode(r.value || new Uint8Array(), { stream: !r.done });
            var idx;
            while ((idx = buf.indexOf("\n")) !== -1) {
              var line = buf.slice(0, idx).trim();
              buf = buf.slice(idx + 1);
              if (!line) continue;
              try { handle(JSON.parse(line)); } catch (e) { /* skip malformed line */ }
            }
            if (r.done) {
              if (buf.trim()) { try { handle(JSON.parse(buf.trim())); } catch (e) {} }
              return;
            }
            return pump();
          });
        }
        return pump();
      }).then(function () {
        reply.classList.remove("is-streaming");
        paint();
        if (!text) reply.textContent = "(no answer)";
      }).catch(function (err) {
        if (err && err.name === "AbortError") {
          reply.classList.remove("is-streaming");
          text += (text ? "\n\n" : "") + "_(stopped)_";
          paint();
        } else {
          fail(err && err.message ? err.message : "Request failed");
        }
      }).then(function () {
        state.controller = null;
        setBusy(false);
        input.focus();
      });
    }

    form.addEventListener("submit", function (evt) {
      evt.preventDefault();
      var pid = (input.getAttribute("data-filled-text") === input.value)
        ? (input.getAttribute("data-filled-id") || null) : null;
      ask(input.value, pid);
    });
    input.addEventListener("keydown", function (evt) {
      if (evt.key !== "Enter") return;
      if (evt.shiftKey) return;
      evt.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit", { cancelable: true }));
    });
    stopBtn.addEventListener("click", function () {
      if (state.controller) state.controller.abort();
    });

    // Initial population (preselection from data-attributes / server render).
    loadTargets().then(updatePlaceholder).catch(function (e) { toast(e.message, "error"); });
    loadPrompts();
    updatePlaceholder();

    return { applyAsk: applyAsk, root: root };
  }

  window.DEL = window.DEL || {};
  var live = null;
  window.DEL.assistant = {
    init: init,
    renderMarkdown: renderMarkdown,
    applyAsk: function (scope, target, rtype, draft) {
      if (live && live.applyAsk) live.applyAsk(scope, target, rtype, draft);
    },
  };
  var page = document.getElementById("assistant-page");
  var dock = document.getElementById("assistant-dock");
  if (page) live = init(page);
  else if (dock) live = init(dock);
})();

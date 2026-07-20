/* DEL admin UI: vanilla JS, no external assets (CSP 'self'). */
(function () {
  "use strict";

  // =========================================================================
  // Value parsing helpers (numeric-aware / date-aware sorting)
  // =========================================================================
  var SIZE_UNITS = {
    b: 1, kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12, pb: 1e15,
    kib: 1024, mib: 1048576, gib: 1073741824, tib: 1099511627776,
    k: 1e3, m: 1e6, g: 1e9, t: 1e12,
  };

  function parseSize(str) {
    var m = /^([0-9]*\.?[0-9]+)\s*([kmgtp]?i?b|[kmgt])?$/i.exec(str.trim());
    if (!m) return null;
    var num = parseFloat(m[1]);
    var unit = (m[2] || "b").toLowerCase();
    var mult = SIZE_UNITS[unit];
    if (mult === undefined) return null;
    return num * mult;
  }

  var ISO_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?/;

  // Return a comparable value for a cell: {n: number} or {s: string}.
  function cellValue(td) {
    var explicit = td.getAttribute("data-sort-value");
    var raw = explicit !== null ? explicit : (td.textContent || "");
    raw = raw.trim();
    if (raw === "" || raw === "—") return { n: null, s: "" };
    // plain number (allow commas, %, leading currency-free)
    var plain = raw.replace(/,/g, "").replace(/%$/, "");
    if (/^-?[0-9]*\.?[0-9]+$/.test(plain)) return { n: parseFloat(plain), s: raw };
    var size = parseSize(raw);
    if (size !== null) return { n: size, s: raw };
    if (ISO_RE.test(raw)) {
      var t = Date.parse(raw.replace(" ", "T"));
      if (!isNaN(t)) return { n: t, s: raw };
    }
    return { n: null, s: raw.toLowerCase() };
  }

  function compareRows(a, b, col, dir) {
    var va = cellValue(a.cells[col]);
    var vb = cellValue(b.cells[col]);
    var res;
    if (va.n !== null && vb.n !== null) {
      res = va.n - vb.n;
    } else if (va.n !== null) {
      res = -1;
    } else if (vb.n !== null) {
      res = 1;
    } else {
      res = va.s < vb.s ? -1 : va.s > vb.s ? 1 : 0;
    }
    return dir === "desc" ? -res : res;
  }

  // =========================================================================
  // Table enhancer
  // =========================================================================
  function enhanceTable(table) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!headRow) return;

    // Collect real data rows; drop any server-rendered empty-state placeholder.
    var allRows = [];
    Array.prototype.forEach.call(tbody.rows, function (r) {
      if (r.hasAttribute("data-empty-row")) {
        r.parentNode.removeChild(r);
      } else {
        allRows.push(r);
      }
    });

    var colCount = headRow.cells.length;

    // --- Build toolbar --------------------------------------------------
    var wrap = document.createElement("div");
    wrap.className = "table-block";
    table.parentNode.insertBefore(wrap, table);

    var toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    wrap.appendChild(toolbar);

    var scroller = document.createElement("div");
    scroller.className = "table-scroll";
    wrap.appendChild(scroller);
    scroller.appendChild(table);

    var status = document.createElement("div");
    status.className = "table-status";
    wrap.appendChild(status);

    var emptyMsg = document.createElement("div");
    emptyMsg.className = "table-empty";
    emptyMsg.hidden = true;
    emptyMsg.textContent = table.getAttribute("data-empty") ||
      "No rows match the current filter.";
    wrap.appendChild(emptyMsg);

    // Search box
    var search = document.createElement("input");
    search.type = "text";
    search.className = "table-filter";
    search.placeholder = table.getAttribute("data-search-placeholder") || "Filter…";
    search.setAttribute("aria-label", "Filter table");
    toolbar.appendChild(search);

    // Dropdown filters, declared via <th data-filter> or data-filter="Label".
    var dropdowns = [];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (!th.hasAttribute("data-filter")) return;
      var values = {};
      allRows.forEach(function (r) {
        var cell = r.cells[idx];
        if (!cell) return;
        var v = (cell.getAttribute("data-filter-value") || cell.textContent).trim();
        if (v && v !== "—") values[v] = true;
      });
      var sel = document.createElement("select");
      sel.className = "table-dropdown";
      var label = th.getAttribute("data-filter") || th.textContent.trim();
      var optAll = document.createElement("option");
      optAll.value = "";
      optAll.textContent = "All " + label.toLowerCase();
      sel.appendChild(optAll);
      Object.keys(values).sort().forEach(function (v) {
        var o = document.createElement("option");
        o.value = v.toLowerCase();
        o.textContent = v;
        sel.appendChild(o);
      });
      sel.dataset.col = idx;
      toolbar.appendChild(sel);
      dropdowns.push(sel);
    });

    // Page-size selector
    var pageSel = document.createElement("select");
    pageSel.className = "table-pagesize";
    pageSel.setAttribute("aria-label", "Rows per page");
    [25, 50, 100, 0].forEach(function (n) {
      var o = document.createElement("option");
      o.value = String(n);
      o.textContent = n === 0 ? "All" : String(n);
      pageSel.appendChild(o);
    });
    var defaultPage = parseInt(table.getAttribute("data-page-size") || "50", 10);
    pageSel.value = String(defaultPage);
    toolbar.appendChild(pageSel);

    // Pager
    var pager = document.createElement("div");
    pager.className = "table-pager";
    var prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "btn btn-sm";
    prevBtn.textContent = "‹ Prev";
    var nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "btn btn-sm";
    nextBtn.textContent = "Next ›";
    pager.appendChild(prevBtn);
    pager.appendChild(nextBtn);
    toolbar.appendChild(pager);

    // --- State ----------------------------------------------------------
    var sortCol = -1, sortDir = "asc";
    var page = 0;
    var filtered = allRows.slice();

    // --- Sorting (click headers) ---------------------------------------
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable");
      th.tabIndex = 0;
      function doSort() {
        if (sortCol === idx) {
          sortDir = sortDir === "asc" ? "desc" : "asc";
        } else {
          sortCol = idx;
          sortDir = "asc";
        }
        Array.prototype.forEach.call(headRow.cells, function (h) {
          h.classList.remove("sort-asc", "sort-desc");
        });
        th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
        render();
      }
      th.addEventListener("click", doSort);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); doSort(); }
      });
    });

    // --- Filtering ------------------------------------------------------
    function applyFilter() {
      var term = search.value.trim().toLowerCase();
      var dd = dropdowns.map(function (s) {
        return { col: parseInt(s.dataset.col, 10), val: s.value };
      });
      filtered = allRows.filter(function (r) {
        if (term && r.textContent.toLowerCase().indexOf(term) === -1) return false;
        for (var i = 0; i < dd.length; i++) {
          if (!dd[i].val) continue;
          var cell = r.cells[dd[i].col];
          var cv = (cell.getAttribute("data-filter-value") || cell.textContent).trim().toLowerCase();
          if (cv !== dd[i].val) return false;
        }
        return true;
      });
      page = 0;
    }

    // --- Render ---------------------------------------------------------
    function render() {
      if (sortCol >= 0) {
        filtered.sort(function (a, b) { return compareRows(a, b, sortCol, sortDir); });
      }
      var size = parseInt(pageSel.value, 10);
      var total = filtered.length;
      var start = 0, end = total;
      if (size > 0) {
        var pages = Math.max(1, Math.ceil(total / size));
        if (page >= pages) page = pages - 1;
        start = page * size;
        end = Math.min(start + size, total);
      }
      // Detach all, then re-append the visible slice in order.
      allRows.forEach(function (r) { if (r.parentNode) r.parentNode.removeChild(r); });
      for (var i = start; i < end; i++) tbody.appendChild(filtered[i]);

      emptyMsg.hidden = total !== 0;
      var shownFrom = total === 0 ? 0 : start + 1;
      var msg = shownFrom + "–" + end + " of " + total;
      if (total !== allRows.length) msg += " (filtered from " + allRows.length + ")";
      status.textContent = msg;

      var hasPages = size > 0 && total > size;
      pager.hidden = !hasPages;
      prevBtn.disabled = page <= 0;
      nextBtn.disabled = size > 0 && end >= total;
    }

    search.addEventListener("input", function () { applyFilter(); render(); });
    dropdowns.forEach(function (s) {
      s.addEventListener("change", function () { applyFilter(); render(); });
    });
    pageSel.addEventListener("change", function () { page = 0; render(); });
    prevBtn.addEventListener("click", function () { if (page > 0) { page--; render(); } });
    nextBtn.addEventListener("click", function () { page++; render(); });

    // Prefill filter from data-prefill (dashboard deep links).
    var prefill = table.getAttribute("data-prefill");
    if (prefill) { search.value = prefill; }

    applyFilter();
    render();
  }

  document.querySelectorAll("table[data-enhanced]").forEach(enhanceTable);

  // =========================================================================
  // Copy-on-click for ids / long values
  // =========================================================================
  document.body.addEventListener("click", function (evt) {
    var el = evt.target.closest("[data-copy]");
    if (!el) return;
    var text = el.getAttribute("data-copy");
    function feedback() {
      var old = el.getAttribute("data-label") || el.textContent;
      el.classList.add("copied");
      var badge = el.querySelector(".copy-mark");
      if (badge) badge.textContent = "✓";
      setTimeout(function () {
        el.classList.remove("copied");
        if (badge) badge.textContent = "⧉";
      }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(feedback, function () {});
    } else {
      var ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); feedback(); } catch (e) {}
      document.body.removeChild(ta);
    }
  });

  // =========================================================================
  // Flash auto-dismiss
  // =========================================================================
  document.querySelectorAll(".flash[data-autodismiss]").forEach(function (f) {
    setTimeout(function () {
      f.style.transition = "opacity .4s";
      f.style.opacity = "0";
      setTimeout(function () { if (f.parentNode) f.parentNode.removeChild(f); }, 450);
    }, 4000);
  });

  // =========================================================================
  // Confirm dialogs for destructive link-buttons
  // =========================================================================
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (evt) {
      if (!window.confirm(el.getAttribute("data-confirm"))) evt.preventDefault();
    });
  });

  // =========================================================================
  // Plan build: "Complete removal (everything)" preset
  // =========================================================================
  var presetComplete = document.getElementById("preset-complete-removal");
  if (presetComplete) {
    presetComplete.addEventListener("change", function () {
      if (!presetComplete.checked) return;
      var namedVolumes = document.getElementById("remove-named-volumes");
      if (namedVolumes) namedVolumes.checked = true;
      document.querySelectorAll(".approved-volume-checkbox").forEach(function (cb) {
        cb.checked = true;
      });
      var removeImages = document.getElementById("remove_images");
      if (removeImages) removeImages.value = "exclusive";
      var bindData = document.getElementById("remove-bind-data");
      if (bindData) bindData.checked = true;
      var repo = document.getElementById("remove-repo");
      if (repo) repo.checked = true;
      var networks = document.getElementById("remove-networks");
      if (networks) networks.checked = true;
      var backup = document.getElementById("backup");
      if (backup) backup.value = "full";
    });
  }

  // =========================================================================
  // Plan execute: typed-phrase gate for live volume deletion
  // =========================================================================
  var modeLive = document.getElementById("mode-live");
  var liveBox = document.getElementById("live-confirm-box");
  var phraseInput = document.getElementById("confirm-phrase");
  var executeBtn = document.getElementById("execute-btn");
  var executeForm = document.getElementById("execute-form");
  var REQUIRED_PHRASE = "DELETE VOLUMES";

  function refreshExecuteGate() {
    if (!executeBtn) return;
    var isLive = modeLive && modeLive.checked;
    if (liveBox) liveBox.hidden = !isLive;
    if (isLive) {
      executeBtn.disabled = !(phraseInput && phraseInput.value === REQUIRED_PHRASE);
    } else {
      executeBtn.disabled = false;
    }
  }

  if (executeForm) {
    executeForm.querySelectorAll('input[name="mode"]').forEach(function (r) {
      r.addEventListener("change", refreshExecuteGate);
    });
    if (phraseInput) phraseInput.addEventListener("input", refreshExecuteGate);
    executeForm.addEventListener("submit", function (evt) {
      var isLive = modeLive && modeLive.checked;
      if (isLive && (!phraseInput || phraseInput.value !== REQUIRED_PHRASE)) {
        evt.preventDefault();
        window.alert('Type "' + REQUIRED_PHRASE + '" to confirm live volume deletion.');
        return;
      }
      if (!window.confirm("Execute this removal plan now? This cannot be undone for irreversible steps.")) {
        evt.preventDefault();
      }
    });
    refreshExecuteGate();
  }

  // =========================================================================
  // Sidebar toggle (responsive top bar under 900px)
  // =========================================================================
  var navToggle = document.getElementById("nav-toggle");
  if (navToggle) {
    navToggle.addEventListener("click", function () {
      document.querySelector(".sidebar").classList.toggle("open");
    });
  }

  // =========================================================================
  // Job status polling + auto-scroll
  // =========================================================================
  var outputBox = document.getElementById("job-output");
  if (outputBox) {
    var jobId = outputBox.getAttribute("data-job-id");
    var statusEl = document.getElementById("job-status");
    var scrollToggle = document.getElementById("autoscroll-toggle");
    var TERMINAL_STATES = ["done", "failed", "success", "error", "refused"];

    function applyStatus(data) {
      if (!data) return;
      if (statusEl && data.status) {
        statusEl.textContent = data.status;
        statusEl.className = "badge status-" + data.status;
      }
      (data.steps || []).forEach(function (step) {
        var row = outputBox.querySelector('tr[data-step-seq="' + step.seq + '"]');
        if (row) {
          var stateCell = row.querySelector(".step-state");
          if (stateCell) {
            stateCell.innerHTML = '<span class="badge status-' + step.state + '">' + step.state + "</span>";
          }
        }
      });
      if (scrollToggle && scrollToggle.checked && outputBox) {
        outputBox.scrollTop = outputBox.scrollHeight;
      }
    }

    function poll() {
      fetch("/jobs/" + jobId + "/status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          applyStatus(data);
          if (!data || TERMINAL_STATES.indexOf(data.status) === -1) {
            setTimeout(poll, 2000);
          }
        })
        .catch(function () { setTimeout(poll, 2000); });
    }
    setTimeout(poll, 2000);
  }
})();

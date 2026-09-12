/* DEL admin UI: vanilla JS, no external assets (CSP 'self'). */
(function () {
  "use strict";

  // =========================================================================
  // Toasts: minimal aria-live notifications (DEL.toast), shared by job
  // completion and copy-to-clipboard feedback.
  // =========================================================================
  function showToast(message, kind) {
    var region = document.getElementById("toast-region");
    if (!region) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + (kind || "info");
    toast.textContent = message;
    region.appendChild(toast);
    setTimeout(function () {
      toast.classList.add("is-leaving");
      setTimeout(function () { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 200);
    }, 3200);
  }
  window.DEL = window.DEL || {};
  window.DEL.toast = showToast;

  // =========================================================================
  // DEL.theme: dark/light toggle, persisted in localStorage. base.html already
  // sets data-theme on <html> before first paint; this wires the header
  // toggle button and keeps its aria-pressed state in sync.
  // =========================================================================
  var THEME_KEY = "del.theme";
  window.DEL.theme = {
    get: function () {
      return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    },
    set: function (theme) {
      document.documentElement.setAttribute("data-theme", theme);
      document.querySelectorAll("meta[data-theme-color]").forEach(function (meta) {
        meta.content = theme === "light" ? "#f3f2ee" : "#1b2021";
      });
      try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
      var btn = document.getElementById("theme-toggle");
      if (btn) {
        var isLight = theme === "light";
        btn.setAttribute("aria-pressed", isLight ? "true" : "false");
        btn.setAttribute("aria-label", isLight ? "Switch to dark theme" : "Switch to light theme");
      }
    },
    toggle: function () {
      window.DEL.theme.set(window.DEL.theme.get() === "light" ? "dark" : "light");
    },
  };
  window.DEL.theme.set(window.DEL.theme.get());
  var themeToggleBtn = document.getElementById("theme-toggle");
  if (themeToggleBtn) themeToggleBtn.addEventListener("click", window.DEL.theme.toggle);

  // Apply data-width (percent) to width style: used by the confidence meter
  // and job progress fill, which the templates set via data attribute so
  // no inline style="" is needed anywhere (CSP-friendly, easy to grep-audit).
  document.querySelectorAll("[data-width]").forEach(function (el) {
    el.style.width = el.getAttribute("data-width") + "%";
  });

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

  // Job durations as rendered server-side: "42s", "1.5m", "2.3h". Checked before
  // parseSize, which would otherwise read "1.5m" as 1.5 MB and sort it above "42s".
  var DURATION_UNITS = { s: 1, m: 60, h: 3600, d: 86400 };

  function parseDuration(str) {
    var m = /^([0-9]*\.?[0-9]+)\s*([smhd])$/.exec(str.trim());
    if (!m) return null;
    return parseFloat(m[1]) * DURATION_UNITS[m[2]];
  }

  var ISO_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?/;

  // Return a comparable value for a raw string: {n: number|null, s: string}.
  // Single source of truth for both the vanilla sort and the AG Grid comparator.
  function sortValue(raw) {
    raw = (raw == null ? "" : String(raw)).trim();
    if (raw === "" || raw === "—") return { n: null, s: "" };
    // plain number (allow commas, %, leading currency-free)
    var plain = raw.replace(/,/g, "").replace(/%$/, "");
    if (/^-?[0-9]*\.?[0-9]+$/.test(plain)) return { n: parseFloat(plain), s: raw };
    var dur = parseDuration(raw);
    if (dur !== null) return { n: dur, s: raw };
    var size = parseSize(raw);
    if (size !== null) return { n: size, s: raw };
    if (ISO_RE.test(raw)) {
      var t = Date.parse(raw.replace(" ", "T"));
      if (!isNaN(t)) return { n: t, s: raw };
    }
    return { n: null, s: raw.toLowerCase() };
  }

  // Return a comparable value for a cell: {n: number} or {s: string}.
  function cellValue(td) {
    var explicit = td.getAttribute("data-sort-value");
    return sortValue(explicit !== null ? explicit : (td.textContent || ""));
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
  // Tables: AG Grid Community when window.agGrid.createGrid is available,
  // otherwise the vanilla engine (sort, Excel-style filter popover, resize,
  // quick filter, pagination, CSV export).
  // =========================================================================
  // Single mobile breakpoint, shared by JS and the CSS @media rules below 900px.
  var MOBILE_MAX = 900;
  var MOBILE_MQ = "(max-width: " + MOBILE_MAX + "px)";

  function matchesMobile() {
    try {
      return !!(window.matchMedia && window.matchMedia(MOBILE_MQ).matches);
    } catch (e) {
      return (window.innerWidth || 0) <= MOBILE_MAX;
    }
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // One popover open at a time; closing it is shared by outside-click, Escape,
  // and opening a different column's popover.
  var openColPopover = null;
  function closeColPopover() {
    if (!openColPopover) return;
    var p = openColPopover;
    openColPopover = null;
    p.trap.release(p.trigger);
    if (p.el.parentNode) p.el.parentNode.removeChild(p.el);
    p.trigger.setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", function (e) {
    if (!openColPopover) return;
    if (openColPopover.el.contains(e.target) || openColPopover.trigger.contains(e.target)) return;
    closeColPopover();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && openColPopover) closeColPopover();
  });

  // Steal the page-level CSV button into the table toolbar so search / filters
  // / clear / export / pagination sit in one place. Secondary control only
  // (never btn-primary — the quick-filter is the single primary control).
  function stealExportButton(table) {
    var id = table && table.id;
    if (!id) return null;
    var btn = null;
    document.querySelectorAll("[data-export-table]").forEach(function (el) {
      if (!btn && el.getAttribute("data-export-table") === id) btn = el;
    });
    if (!btn || btn.getAttribute("data-del-toolbar-export") === "1") return null;
    var info = { el: btn, parent: btn.parentNode, next: btn.nextSibling };
    btn.classList.remove("btn-primary");
    if (!/\bbtn\b/.test(btn.className)) btn.className = (btn.className + " btn btn-sm").trim();
    btn.setAttribute("data-del-toolbar-export", "1");
    return info;
  }
  function restoreStolenNode(info) {
    if (!info || !info.el || !info.parent) return;
    info.parent.insertBefore(info.el, info.next);
    info.el.removeAttribute("data-del-toolbar-export");
  }

  // Vanilla table engine (sort, per-column Excel-style filter, quick filter,
  // resize, pagination, CSV export). Used when AG Grid is missing or createGrid
  // throws; also the path for environments without the vendored script.
  function enhanceTableVanilla(table) {
    var tbody = table.tBodies[0];
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!tbody || !headRow) return;

    var allRows = Array.prototype.slice.call(tbody.rows);

    var wrap = document.createElement("div");
    wrap.className = "table-block";
    table.parentNode.insertBefore(wrap, table);

    var toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    wrap.appendChild(toolbar);

    var filterPills = document.createElement("div");
    filterPills.className = "active-filter-pills";
    filterPills.hidden = true;
    wrap.appendChild(filterPills);

    var scroller = document.createElement("div");
    scroller.className = "table-scroll";
    wrap.appendChild(scroller);
    scroller.appendChild(table);

    var status = document.createElement("div");
    status.className = "table-status";
    status.setAttribute("role", "status");

    var emptyMsg = document.createElement("div");
    emptyMsg.className = "empty-state";
    emptyMsg.hidden = true;
    var emptyMsgText = document.createElement("div");
    emptyMsgText.className = "empty-state-msg";
    emptyMsgText.textContent = table.getAttribute("data-empty") || "No rows match the current filter.";
    emptyMsg.appendChild(emptyMsgText);
    wrap.appendChild(emptyMsg);

    var search = document.createElement("input");
    search.type = "text";
    search.className = "table-filter";
    search.placeholder = table.getAttribute("data-search-placeholder") || "Quick filter…";
    search.setAttribute("aria-label", "Quick filter all columns");
    toolbar.appendChild(search);

    var densityBtn = document.createElement("button");
    densityBtn.type = "button";
    densityBtn.className = "btn btn-sm table-density";
    function currentDensity() {
      return document.documentElement.getAttribute("data-density") === "compact" ? "compact" : "comfortable";
    }
    function setDensity(mode) {
      document.documentElement.setAttribute("data-density", mode);
      try { localStorage.setItem("del-density", mode); } catch (err) {}
      densityBtn.textContent = mode === "compact" ? "Comfortable" : "Compact";
      densityBtn.setAttribute("aria-pressed", mode === "compact" ? "true" : "false");
    }
    setDensity(currentDensity() === "compact" || (function () {
      try { return localStorage.getItem("del-density") === "compact"; } catch (err) { return false; }
    })() ? "compact" : "comfortable");
    densityBtn.addEventListener("click", function () {
      setDensity(currentDensity() === "compact" ? "comfortable" : "compact");
    });
    toolbar.appendChild(densityBtn);

    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn btn-sm table-clear-filters";
    clearBtn.textContent = "Clear all filters";
    toolbar.appendChild(clearBtn);

    // Mobile only: `data-priority="low"` columns are hidden below 900px by
    // default (see CSS); this toggle reveals them without going back to a
    // horizontal-scroll table for every column.
    var hasLowPriority = !!headRow.querySelector('[data-priority="low"]');
    var showAllBtn = null;
    if (hasLowPriority) {
      showAllBtn = document.createElement("button");
      showAllBtn.type = "button";
      showAllBtn.className = "btn btn-sm table-show-all";
      showAllBtn.textContent = "Show all columns";
      showAllBtn.setAttribute("aria-pressed", "false");
      table.setAttribute("data-hide-low", "");
      showAllBtn.addEventListener("click", function () {
        var hidden = table.hasAttribute("data-hide-low");
        if (hidden) {
          table.removeAttribute("data-hide-low");
          showAllBtn.textContent = "Hide secondary columns";
          showAllBtn.setAttribute("aria-pressed", "true");
        } else {
          table.setAttribute("data-hide-low", "");
          showAllBtn.textContent = "Show all columns";
          showAllBtn.setAttribute("aria-pressed", "false");
        }
      });
      toolbar.appendChild(showAllBtn);
    }

    // Per-column filter: a header button opens an Excel-style popover (text
    // operator + multi-select of existing values), replacing the always-visible
    // AG Grid floating-filter row.
    var colFilters = [];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      var headerLabel = (th.textContent || "").trim();
      var values = {};
      allRows.forEach(function (r) {
        var cell = r.cells[idx];
        if (!cell) return;
        var v = (cell.getAttribute("data-filter-value") || cell.textContent || "").replace(/\s+/g, " ").trim();
        if (v === "—") v = "";
        values[v] = true;
      });
      var valueList = Object.keys(values).sort();

      var filterBtn = document.createElement("button");
      filterBtn.type = "button";
      filterBtn.className = "th-filter-btn";
      filterBtn.setAttribute("aria-label", "Filter " + headerLabel);
      filterBtn.setAttribute("aria-haspopup", "dialog");
      filterBtn.setAttribute("aria-expanded", "false");
      filterBtn.innerHTML = '<span aria-hidden="true">⧩</span>';
      th.appendChild(filterBtn);

      var state = { op: "contains", val: "", selected: null, exclude: false, col: idx, label: headerLabel };
      colFilters.push(state);

      function isActive() {
        var numeric = state.op.indexOf("gt") === 0 || state.op.indexOf("lt") === 0;
        return !!(state.val.trim()) || state.selected !== null || state.op === "blank" || state.op === "notBlank" || (numeric && state.val.trim());
      }
      function updateBtn() {
        filterBtn.classList.toggle("is-active", isActive());
        filterBtn.title = isActive() ? "Filter on (click to edit)" : "Filter this column like Excel";
      }

      function openPopover() {
        if (openColPopover && openColPopover.trigger === filterBtn) { closeColPopover(); return; }
        closeColPopover();

        var pop = document.createElement("div");
        pop.className = "col-filter-pop";
        pop.setAttribute("role", "dialog");
        pop.setAttribute("aria-label", "Filter " + headerLabel);
        pop.innerHTML =
          '<div class="col-filter-pop-title">Filter “' + escapeHtml(headerLabel) + '”</div>' +
          '<div class="col-filter-pop-head">' +
          '<label>Show rows where this column' +
          '<select class="col-filter-op" aria-label="Filter operator for ' + escapeHtml(headerLabel) + '">' +
          '<option value="contains">contains</option>' +
          '<option value="notContains">does not contain</option>' +
          '<option value="equals">equals</option>' +
          '<option value="notEqual">does not equal</option>' +
          '<option value="startsWith">starts with</option>' +
          '<option value="endsWith">ends with</option>' +
          '<option value="gt">greater than</option>' +
          '<option value="gte">greater than or equal</option>' +
          '<option value="lt">less than</option>' +
          '<option value="lte">less than or equal</option>' +
          '<option value="blank">is empty</option>' +
          '<option value="notBlank">is not empty</option>' +
          "</select>" +
          '<input type="text" class="col-filter-text" placeholder="Type a value…" aria-label="Filter text for ' + escapeHtml(headerLabel) + '">' +
          "</label></div>" +
          '<label class="col-filter-exclude"><input type="checkbox" class="col-filter-exclude-cb"> Exclude selected values</label>' +
          '<div class="col-filter-actions">' +
          '<button type="button" class="btn btn-sm col-filter-all">(Select All)</button>' +
          '<button type="button" class="btn btn-sm col-filter-none">Clear list</button>' +
          "</div>" +
          '<input type="search" class="col-filter-search" placeholder="Search values in this column…" aria-label="Search values for ' + escapeHtml(headerLabel) + '">' +
          '<div class="col-filter-list" role="group" aria-label="Values"></div>' +
          '<div class="col-filter-close-row"><button type="button" class="btn btn-sm btn-primary col-filter-done">Apply</button></div>';

        var opEl = pop.querySelector(".col-filter-op");
        var textEl = pop.querySelector(".col-filter-text");
        var searchEl = pop.querySelector(".col-filter-search");
        var listEl = pop.querySelector(".col-filter-list");
        opEl.value = state.op;
        textEl.value = state.val;
        var excludeEl = pop.querySelector(".col-filter-exclude-cb");
        if (excludeEl) excludeEl.checked = !!state.exclude;
        var needsText = function () { return opEl.value !== "blank" && opEl.value !== "notBlank"; };
        textEl.disabled = !needsText();

        function renderList() {
          var q = (searchEl.value || "").toLowerCase();
          listEl.innerHTML = "";
          var allOn = state.selected === null;
          valueList.forEach(function (v) {
            var labelText = v === "" ? "(blank)" : v;
            if (q && labelText.toLowerCase().indexOf(q) === -1) return;
            var lab = document.createElement("label");
            lab.className = "col-filter-item";
            var cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = allOn || (state.selected && state.selected.has(v));
            cb.addEventListener("change", function () {
              if (state.selected === null) state.selected = new Set(valueList);
              if (cb.checked) state.selected.add(v); else state.selected.delete(v);
              if (state.selected.size === valueList.length) state.selected = null;
              applyFilter(); render(); updateBtn(); updateFilterPills();
            });
            var span = document.createElement("span");
            span.textContent = labelText;
            span.title = labelText;
            lab.appendChild(cb);
            lab.appendChild(span);
            listEl.appendChild(lab);
          });
          if (!listEl.childNodes.length) {
            var empty = document.createElement("div");
            empty.className = "muted col-filter-empty";
            empty.textContent = "No values";
            listEl.appendChild(empty);
          }
        }
        renderList();

        pop.querySelector(".col-filter-all").addEventListener("click", function () {
          state.selected = null; renderList(); applyFilter(); render(); updateBtn(); updateFilterPills();
        });
        pop.querySelector(".col-filter-none").addEventListener("click", function () {
          state.selected = new Set(); renderList(); applyFilter(); render(); updateBtn(); updateFilterPills();
        });
        if (excludeEl) excludeEl.addEventListener("change", function () {
          state.exclude = excludeEl.checked; applyFilter(); render(); updateBtn(); updateFilterPills();
        });
        opEl.addEventListener("change", function () {
          state.op = opEl.value;
          textEl.disabled = !needsText();
          if (textEl.disabled) { textEl.value = ""; state.val = ""; }
          applyFilter(); render(); updateBtn(); updateFilterPills();
        });
        textEl.addEventListener("input", function () {
          state.val = textEl.value; applyFilter(); render(); updateBtn(); updateFilterPills();
        });
        searchEl.addEventListener("input", renderList);
        pop.querySelector(".col-filter-done").addEventListener("click", closeColPopover);

        document.body.appendChild(pop);
        var r = filterBtn.getBoundingClientRect();
        pop.style.position = "absolute";
        pop.style.top = (window.scrollY + r.bottom + 4) + "px";
        var left = window.scrollX + r.left;
        var maxLeft = window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8;
        pop.style.left = Math.max(8, Math.min(left, maxLeft)) + "px";

        var trap = makeFocusTrap(function () { return pop; });
        filterBtn.setAttribute("aria-expanded", "true");
        openColPopover = { el: pop, trigger: filterBtn, trap: trap };
        trap.activate();
      }
      filterBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        openPopover();
      });
      updateBtn();

      // Column resize: drag the header's trailing edge.
      var resizer = document.createElement("span");
      resizer.className = "th-resizer";
      resizer.setAttribute("aria-hidden", "true");
      th.appendChild(resizer);
      resizer.addEventListener("mousedown", function (e) {
        e.preventDefault();
        var startX = e.clientX;
        var startW = th.offsetWidth;
        table.style.tableLayout = "fixed";
        function onMove(ev) {
          var w = Math.max(60, startW + (ev.clientX - startX));
          th.style.width = w + "px";
        }
        function onUp() {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    });

    var chipFilters = {};
    var chipBar = document.createElement("div");
    chipBar.className = "filter-chipbar";
    chipBar.hidden = true;
    wrap.insertBefore(chipBar, scroller);
    var quickHeaders = /^(status|kind|mode|shared|protected|state|state\/health|orphan|dangling|health)$/i;
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      var headerLabel = (th.textContent || "").replace(/▾.*/g, "").trim();
      if (!quickHeaders.test(headerLabel)) return;
      var values = {};
      allRows.forEach(function (r) {
        var cell = r.cells[idx];
        if (!cell) return;
        var v = (cell.getAttribute("data-filter-value") || cell.textContent || "").replace(/\s+/g, " ").trim();
        if (!v || v === "—") return;
        v.split(/\s+/).forEach(function (part) {
          if (part && part !== "—") values[part] = true;
        });
      });
      var keys = Object.keys(values).sort();
      if (keys.length < 2 || keys.length > 12) return;
      var group = document.createElement("div");
      group.className = "filter-chip-group";
      var glabel = document.createElement("span");
      glabel.className = "filter-chip-label";
      glabel.textContent = headerLabel;
      group.appendChild(glabel);
      keys.forEach(function (val) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "filter-chip";
        chip.setAttribute("aria-pressed", "false");
        chip.textContent = val;
        chip.addEventListener("click", function () {
          var set = chipFilters[idx] || new Set();
          if (set.has(val)) set.delete(val); else set.add(val);
          if (set.size === 0) delete chipFilters[idx]; else chipFilters[idx] = set;
          chip.setAttribute("aria-pressed", set.has(val) ? "true" : "false");
          chip.classList.toggle("is-on", set.has(val));
          applyFilter(); render();
        });
        group.appendChild(chip);
      });
      chipBar.appendChild(group);
      chipBar.hidden = false;
    });

    var pageSel = document.createElement("select");
    pageSel.className = "table-pagesize";
    [25, 50, 100, 0].forEach(function (n) {
      var o = document.createElement("option");
      o.value = String(n);
      o.textContent = n === 0 ? "All" : String(n);
      pageSel.appendChild(o);
    });
    pageSel.value = String(parseInt(table.getAttribute("data-page-size") || "50", 10));
    toolbar.appendChild(pageSel);

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
    toolbar.appendChild(status);
    var stolenExport = stealExportButton(table);
    if (stolenExport) toolbar.appendChild(stolenExport.el);

    var sortCol = -1, sortDir = "asc", page = 0, filtered = allRows.slice();

    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (th.hasAttribute("data-nosort")) return;
      th.classList.add("sortable");
      // aria-sort belongs on the columnheader; the button carries the click
      // affordance. Putting role="button" on the <th> itself would drop the
      // columnheader role that makes aria-sort meaningful.
      th.setAttribute("aria-sort", "none");
      var trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "th-sort-btn";
      trigger.innerHTML = th.innerHTML;
      trigger.setAttribute("aria-label", "Sort by " + (th.textContent || "").trim());
      th.innerHTML = "";
      th.appendChild(trigger);
      function doSort() {
        if (sortCol === idx) sortDir = sortDir === "asc" ? "desc" : "asc";
        else { sortCol = idx; sortDir = "asc"; }
        Array.prototype.forEach.call(headRow.cells, function (h) {
          h.classList.remove("sort-asc", "sort-desc");
          if (h.hasAttribute("aria-sort")) h.setAttribute("aria-sort", "none");
        });
        th.classList.add(sortDir === "asc" ? "sort-asc" : "sort-desc");
        th.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
        render();
      }
      trigger.addEventListener("click", doSort);
    });

    function cellText(r, col) {
      var cell = r.cells[col];
      if (!cell) return "";
      return (cell.getAttribute("data-filter-value") || cell.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
    }

    function matchOp(text, op, val) {
      val = (val || "").toLowerCase();
      if (op === "blank") return !text || text === "—";
      if (op === "notBlank") return !!(text && text !== "—");
      if (!val) return true;
      if (op === "contains") return text.indexOf(val) !== -1;
      if (op === "notContains") return text.indexOf(val) === -1;
      if (op === "equals") return text === val;
      if (op === "notEqual") return text !== val;
      if (op === "startsWith") return text.indexOf(val) === 0;
      if (op === "endsWith") return text.length >= val.length && text.slice(-val.length) === val;
      return true;
    }

    function cellNum(r, col) {
      var cell = r.cells[col];
      if (!cell) return NaN;
      var s = cell.getAttribute("data-sort-value");
      if (s == null || s === "") s = (cell.textContent || "").replace(/,/g, "").trim();
      return parseFloat(s);
    }

    function updateFilterPills() {
      filterPills.innerHTML = "";
      var any = false;
      colFilters.forEach(function (f) {
        var parts = [];
        if (f.op === "blank" || f.op === "notBlank") parts.push(f.op === "blank" ? "is empty" : "is not empty");
        else if (f.val.trim()) parts.push(f.op + " “" + f.val.trim() + "”");
        if (f.selected !== null) parts.push((f.exclude ? "exclude " : "") + f.selected.size + " values");
        if (!parts.length) return;
        any = true;
        var pill = document.createElement("button");
        pill.type = "button";
        pill.className = "filter-chip is-on";
        pill.textContent = (f.label || ("Col " + (f.col + 1))) + ": " + parts.join(", ") + " ×";
        pill.setAttribute("aria-label", "Clear filter on " + (f.label || "column"));
        pill.addEventListener("click", function () {
          f.op = "contains"; f.val = ""; f.selected = null; f.exclude = false;
          wrap.querySelectorAll(".th-filter-btn.is-active").forEach(function (b) { /* refreshed below */ });
          var btns = wrap.querySelectorAll(".th-filter-btn");
          if (btns[f.col]) btns[f.col].classList.remove("is-active");
          applyFilter(); render(); updateFilterPills();
        });
        filterPills.appendChild(pill);
      });
      filterPills.hidden = !any;
    }

    function applyFilter() {
      var term = search.value.trim().toLowerCase();
      filtered = allRows.filter(function (r) {
        if (term && r.textContent.toLowerCase().indexOf(term) === -1) return false;
        for (var chipCol in chipFilters) {
          if (!Object.prototype.hasOwnProperty.call(chipFilters, chipCol)) continue;
          var wanted = chipFilters[chipCol];
          var rawChip = cellText(r, Number(chipCol));
          var okChip = false;
          wanted.forEach(function (v) {
            if (rawChip.indexOf(String(v).toLowerCase()) !== -1) okChip = true;
          });
          if (!okChip) return false;
        }
        for (var i = 0; i < colFilters.length; i++) {
          var f = colFilters[i];
          var text = cellText(r, f.col);
          // Excel-style value set
          if (f.selected !== null) {
            var raw = (text === "—" ? "" : text);
            var hit = false;
            f.selected.forEach(function (v) { if (String(v).toLowerCase() === raw) hit = true; });
            if (f.exclude) { if (hit) return false; }
            else if (!hit) return false;
          }
          var op = f.op;
          var val = f.val;
          if (op === "gt" || op === "gte" || op === "lt" || op === "lte") {
            if (!val.trim()) continue;
            var n = cellNum(r, f.col);
            var cmp = parseFloat(val);
            if (isNaN(cmp) || isNaN(n)) return false;
            if (op === "gt" && !(n > cmp)) return false;
            if (op === "gte" && !(n >= cmp)) return false;
            if (op === "lt" && !(n < cmp)) return false;
            if (op === "lte" && !(n <= cmp)) return false;
            continue;
          }
          if (op !== "blank" && op !== "notBlank" && !val.trim()) continue;
          if (!matchOp(text, op, val.trim())) return false;
        }
        return true;
      });
      page = 0;
    }

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
      allRows.forEach(function (r) { if (r.parentNode) r.parentNode.removeChild(r); });
      for (var i = start; i < end; i++) tbody.appendChild(filtered[i]);
      emptyMsg.hidden = total !== 0;
      var shownFrom = total === 0 ? 0 : start + 1;
      var msg = shownFrom + "–" + end + " of " + total;
      if (total !== allRows.length) msg += " (filtered from " + allRows.length + ")";
      status.textContent = msg;
      updateFilterPills();
      var hasPages = size > 0 && total > size;
      pager.hidden = !hasPages;
      prevBtn.disabled = page <= 0;
      nextBtn.disabled = size > 0 && end >= total;
    }

    search.addEventListener("input", function () { applyFilter(); render(); });
    clearBtn.addEventListener("click", function () {
      search.value = "";
      closeColPopover();
      colFilters.forEach(function (f) {
        f.op = "contains";
        f.val = "";
        f.selected = null;
        f.exclude = false;
      });
      Object.keys(chipFilters).forEach(function (k) { delete chipFilters[k]; });
      wrap.querySelectorAll(".filter-chip.is-on").forEach(function (c) {
        c.classList.remove("is-on");
        c.setAttribute("aria-pressed", "false");
      });
      wrap.querySelectorAll(".th-filter-btn.is-active").forEach(function (b) {
        b.classList.remove("is-active");
      });
      applyFilter();
      render();
    });
    pageSel.addEventListener("change", function () { page = 0; render(); });
    prevBtn.addEventListener("click", function () { if (page > 0) { page--; render(); } });
    nextBtn.addEventListener("click", function () { page++; render(); });

    // Pagination physically removes off-page rows, so CSV export has to ask for
    // the filtered set rather than reading what is currently in the DOM.
    table._delFilteredRows = function () { return filtered; };

    var prefill = table.getAttribute("data-prefill");
    if (prefill) search.value = prefill;
    applyFilter();
    render();
  }

  function applyAgThemeClass(host) {
    var isDark = document.documentElement.getAttribute("data-theme") !== "light";
    host.classList.remove("ag-theme-quartz", "ag-theme-quartz-dark");
    host.classList.add(isDark ? "ag-theme-quartz-dark" : "ag-theme-quartz");
    host.classList.add("del-ag-grid");
  }

  // Persist column chrome only (order/width/sort/filter/page size). Never row data.
  function tableStateKey(table) {
    var id = (table && table.id) ? String(table.id).trim() : "";
    if (id) return "del.ag2.colstate." + id;
    var path = "";
    try { path = (location.pathname || ""); } catch (e) { path = ""; }
    var cap = "";
    var block = table && table.closest ? table.closest("section, article, .card, main") : null;
    var capEl = block && block.querySelector ? block.querySelector("caption, h1, h2, h3, legend") : null;
    if (table && table.caption) cap = (table.caption.textContent || "").trim();
    else if (capEl) cap = (capEl.textContent || "").trim();
    return "del.ag2.colstate." + path + "|" + cap;
  }

  function loadTableColState(table) {
    try {
      var raw = localStorage.getItem(tableStateKey(table));
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return null;
      if (parsed.rowData) delete parsed.rowData;
      return parsed;
    } catch (e) {
      return null;
    }
  }

  function saveTableColState(table, api, pageSel) {
    if (!api) return;
    var payload = {};
    try {
      if (typeof api.getColumnState === "function") payload.columnState = api.getColumnState();
    } catch (e1) {}
    try {
      if (typeof api.getFilterModel === "function") payload.filterModel = api.getFilterModel();
    } catch (e2) {}
    try {
      var n = pageSel ? parseInt(pageSel.value, 10) : 0;
      if (!n && typeof api.paginationGetPageSize === "function") n = api.paginationGetPageSize();
      if (n) payload.pageSize = n;
    } catch (e3) {}
    try {
      localStorage.setItem(tableStateKey(table), JSON.stringify(payload));
    } catch (e4) {}
  }

  function restoreTableColState(table, api, pageSel) {
    var saved = loadTableColState(table);
    if (!saved || !api) return;
    try {
      if (saved.columnState && typeof api.applyColumnState === "function") {
        api.applyColumnState({ state: saved.columnState, applyOrder: true });
      }
    } catch (e1) {}
    try {
      if (saved.filterModel && typeof api.setFilterModel === "function") {
        api.setFilterModel(saved.filterModel);
      }
    } catch (e2) {}
    if (saved.pageSize) {
      var n = parseInt(saved.pageSize, 10);
      if (n > 0) {
        try { api.setGridOption("paginationPageSize", n); } catch (e3) {}
        if (pageSel) {
          var opt = pageSel.querySelector('option[value="' + n + '"]');
          if (opt) pageSel.value = String(n);
        }
      }
    }
  }

  function enhanceTableAgGrid(table) {
    var ag = window.agGrid;
    if (!ag || typeof ag.createGrid !== "function") return false;
    if (table._delGridApi) return true;
    var tbody = table.tBodies[0];
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!tbody || !headRow) return false;

    var origParent = table.parentNode;
    var origNext = table.nextSibling;

    var wrap = document.createElement("div");
    wrap.className = "table-block ag-host-wrap";
    origParent.insertBefore(wrap, table);

    var toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    wrap.appendChild(toolbar);
    var search = document.createElement("input");
    search.type = "text";
    search.className = "table-filter";
    search.placeholder = table.getAttribute("data-search-placeholder") || "Quick filter…";
    search.setAttribute("aria-label", "Quick filter all columns");
    toolbar.appendChild(search);
    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn btn-sm table-clear-filters";
    clearBtn.textContent = "Clear all filters";
    toolbar.appendChild(clearBtn);
    var stolenExport = stealExportButton(table);
    if (stolenExport) toolbar.appendChild(stolenExport.el);
    var pageSel = document.createElement("select");
    pageSel.className = "table-pagesize";
    pageSel.setAttribute("aria-label", "Rows per page");
    [25, 50, 100, 250].forEach(function (n) {
      var o = document.createElement("option");
      o.value = String(n);
      o.textContent = String(n);
      pageSel.appendChild(o);
    });
    toolbar.appendChild(pageSel);
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
    var status = document.createElement("div");
    status.className = "table-status";
    status.setAttribute("role", "status");
    toolbar.appendChild(status);

    var host = document.createElement("div");
    applyAgThemeClass(host);
    wrap.appendChild(host);

    var NUM_RE = /^-?\d+(\.\d+)?$/;
    // Cells holding block content (meters, disclosures, forms, several chips)
    // cannot live in a fixed 36px row: those columns wrap and grow the row.
    var RICH_SEL = "details, ul, ol, form, .confidence, .cluster, div, pre, br";
    var rows = Array.prototype.slice.call(tbody.rows);
    var sample = rows.slice(0, 200);
    var columnDefs = [];
    var rowData = [];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      var header = (th.textContent || "").replace(/[\u25be\u21e9]/g, "").trim();
      var nosort = th.hasAttribute("data-nosort");
      var numeric = false;
      var rich = false;
      var pills = false;
      var lens = [];
      sample.forEach(function (tr) {
        var cell = tr.cells[idx];
        if (!cell) return;
        var sv = cell.getAttribute("data-sort-value");
        if (sv && NUM_RE.test(sv)) numeric = true;
        if (!rich && cell.querySelector(RICH_SEL)) rich = true;
        if (!pills && cell.querySelector(".badge, .chip")) pills = true;
        // Measure what is visible: text inside a <details> body is collapsed.
        var len = (cell.textContent || "").replace(/\s+/g, " ").trim().length;
        cell.querySelectorAll("details > :not(summary)").forEach(function (el) {
          len -= (el.textContent || "").replace(/\s+/g, " ").trim().length;
        });
        lens.push(Math.max(0, len));
      });
      // Width from content: header must fit (uppercase ~7.4px/char + sort and
      // filter icons), body gets the 80th-percentile text length.
      lens.sort(function (a, b) { return a - b; });
      // 90th percentile: names have a long tail the median would wrap.
      var p80 = lens.length ? lens[Math.min(lens.length - 1, Math.floor(lens.length * 0.9))] : 0;
      var headerW = Math.round(header.length * 7.4 + (nosort ? 28 : 58));
      // Badges/chips are bold + padded: ~8px per char plus the pill padding.
      var bodyW = Math.round(Math.min(p80, 40) * (pills ? 8 : 7) + (pills ? 44 : 28));
      // Minimum = header, or the body text up to ~24 characters (rich cells
      // wrap, so they need less); the rest of the width is shared by flex.
      var minW = Math.max(72, Math.min(headerW, 240), Math.min(bodyW, rich ? 180 : 196));
      var idealW = Math.max(minW, bodyW);
      columnDefs.push({
        colId: "c" + idx,
        field: "c" + idx,
        headerName: header || ("Col " + (idx + 1)),
        headerTooltip: th.getAttribute("title") || header,
        sortable: !nosort,
        filter: nosort ? false : (numeric ? "agNumberColumnFilter" : "agTextColumnFilter"),
        suppressHeaderMenuButton: true,
        suppressHeaderFilterButton: nosort,
        suppressMovable: nosort,
        pinned: nosort && idx === headRow.cells.length - 1 ? "right" : null,
        lockPinned: nosort,
        // Action columns (data-nosort) keep a fixed width; data columns flex.
        minWidth: minW,
        width: nosort ? idealW : undefined,
        // The first column names the row; give it a larger share of spare width.
        flex: nosort ? null : Math.max(1, Math.round((idx === 0 ? idealW * 1.5 : idealW) / 90)),
        wrapText: rich,
        autoHeight: rich,
        cellClass: rich ? "ag-cell-rich" : null,
        tooltipValueGetter: rich ? null : function (params) {
          var v = params.data ? params.data["t" + idx] : "";
          return v && v.length > 18 ? v : null;
        },
        filterParams: numeric ? {
          filterOptions: ["equals", "notEqual", "lessThan", "greaterThan", "inRange", "blank", "notBlank"],
          defaultOption: "equals",
          buttons: ["reset", "apply"],
          closeOnApply: true,
        } : {
          filterOptions: ["contains", "notContains", "equals", "startsWith", "endsWith", "blank"],
          defaultOption: "contains",
          buttons: ["reset", "apply"],
          closeOnApply: true,
        },
        comparator: function (a, b, nodeA, nodeB) {
          var sa = nodeA && nodeA.data ? nodeA.data["s" + idx] : a;
          var sb = nodeB && nodeB.data ? nodeB.data["s" + idx] : b;
          if (sa == null) sa = "";
          if (sb == null) sb = "";
          if (typeof sa === "number" && typeof sb === "number") return sa - sb;
          if (typeof sa === "number") return -1;
          if (typeof sb === "number") return 1;
          if (sa < sb) return -1;
          if (sa > sb) return 1;
          return 0;
        },
        cellRenderer: function (params) {
          var span = document.createElement("span");
          span.className = "ag-cell-inner";
          span.innerHTML = (params.data && params.data["h" + idx]) || "";
          return span;
        },
      });
    });
    Array.prototype.forEach.call(tbody.rows, function (tr) {
      var row = { _tr: tr };
      Array.prototype.forEach.call(headRow.cells, function (_th, idx) {
        var cell = tr.cells[idx];
        var text = cell ? (cell.getAttribute("data-filter-value") || cell.textContent || "").replace(/\s+/g, " ").trim() : "";
        var sortRaw = cell ? cell.getAttribute("data-sort-value") : "";
        var sortVal = sortRaw && NUM_RE.test(sortRaw) ? parseFloat(sortRaw) : (text || "").toLowerCase();
        var filterVal = text === "—" ? "" : text;
        // Original <td> stays in the hidden table with data-sort-value /
        // data-filter-value / data-priority intact; snapshot them on the row
        // so export and comparators never depend on AG Grid dropping attrs.
        row["c" + idx] = (sortRaw && NUM_RE.test(sortRaw)) ? parseFloat(sortRaw) : filterVal;
        row["t" + idx] = cell ? (cell.getAttribute("title") || cell.textContent || "").replace(/\s+/g, " ").trim() : "";
        row["s" + idx] = sortVal;
        row["h" + idx] = cell ? cell.innerHTML : "";
        row["ds" + idx] = cell ? cell.getAttribute("data-sort-value") : null;
        row["df" + idx] = cell ? cell.getAttribute("data-filter-value") : null;
        row["dp" + idx] = cell ? cell.getAttribute("data-priority") : null;
      });
      rowData.push(row);
    });

    // Fit before scrolling: if the minimum widths overflow the available
    // width, shrink the flexible columns proportionally (headers keep a
    // tooltip). A grid inside a collapsed <details> measures 0, so use the
    // nearest visible ancestor, minus a section's padding.
    var avail = wrap.clientWidth;
    if (!avail) {
      var anc = wrap.parentElement;
      while (anc && !anc.clientWidth) anc = anc.parentElement;
      avail = anc ? anc.clientWidth - 48 : 0;
    }
    if (avail > 240) {
      // Short columns (badges, counts, dates) keep their width; only wide
      // text columns (> 130px) give some up, never below 110px.
      var fixedW = 0, wideMin = 0;
      columnDefs.forEach(function (c) {
        if (c.width || c.minWidth <= 130) fixedW += (c.width || c.minWidth);
        else wideMin += c.minWidth;
      });
      var room = avail - fixedW - 4;
      if (wideMin > 0 && room > 0 && wideMin > room) {
        var f = room / wideMin;
        columnDefs.forEach(function (c) {
          if (!c.width && c.minWidth > 130) c.minWidth = Math.max(110, Math.floor(c.minWidth * f));
        });
      }
    }

    var pageSize = parseInt(table.getAttribute("data-page-size") || "50", 10);
    if (!pageSize || pageSize < 1) pageSize = 50;
    if (!pageSel.querySelector('option[value="' + pageSize + '"]')) {
      var extra = document.createElement("option");
      extra.value = String(pageSize);
      extra.textContent = String(pageSize);
      pageSel.insertBefore(extra, pageSel.firstChild);
    }
    pageSel.value = String(pageSize);
    // Short tables get no search / pager chrome — just the rows.
    var minimal = rowData.length <= 10 && !table.hasAttribute("data-toolbar");
    if (minimal) toolbar.classList.add("is-minimal");
    var compact = table.getAttribute("data-density") === "compact" ||
      document.documentElement.getAttribute("data-density") === "compact";
    var api;
    var persist = function () {};
    try {
      api = ag.createGrid(host, {
        columnDefs: columnDefs,
        rowData: rowData,
        defaultColDef: {
          sortable: true,
          filter: true,
          floatingFilter: false,
          // Long headers wrap to a second line instead of truncating.
          wrapHeaderText: true,
          autoHeaderHeight: true,
          resizable: true,
          minWidth: 72,
          flex: 1,
          suppressMovable: false,
          lockPinned: false,
        },
        // Page height, not a fixed box: no empty band under short tables and
        // no scroll-inside-scroll. Pagination bounds the height of long ones.
        domLayout: "autoHeight",
        columnMenu: "new",
        suppressMenuHide: true,
        suppressMovableColumns: false,
        animateRows: false,
        pagination: true,
        paginationPageSize: pageSize,
        suppressPaginationPanel: true,
        rowBuffer: 10,
        suppressCellFocus: false,
        enableCellTextSelection: true,
        ensureDomOrder: true,
        tooltipShowDelay: 500,
        tooltipInteraction: true,
        enterNavigatesVertically: true,
        enterNavigatesVerticallyAfterEdit: true,
        headerHeight: compact ? 30 : 38,
        rowHeight: compact ? 30 : 38,
        overlayNoRowsTemplate: "<div class=\"empty-state-msg\">" +
          (table.getAttribute("data-empty") || "No rows match the current filter.") + "</div>",
        onFilterChanged: function () { updateStatus(); persist(); },
        onSortChanged: function () { updateStatus(); persist(); },
        onPaginationChanged: function () { updateStatus(); },
        onColumnMoved: function (ev) { if (!ev || ev.finished !== false) persist(); },
        onColumnResized: function (ev) { if (!ev || ev.finished !== false) persist(); },
        onColumnPinned: function () { persist(); },
        onColumnVisible: function () { persist(); },
      });
    } catch (err) {
      restoreStolenNode(stolenExport);
      if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      if (table.parentNode !== origParent) {
        origParent.insertBefore(table, origNext);
      }
      table.style.display = "";
      table._delGridApi = null;
      return false;
    }
    if (!api) {
      restoreStolenNode(stolenExport);
      if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      table.style.display = "";
      return false;
    }

    persist = function () { saveTableColState(table, api, pageSel); };
    restoreTableColState(table, api, pageSel);
    if (!pageSel.value) pageSel.value = String(pageSize);
    function updateStatus() {
      var total = rowData.length;
      var afterFilter = total;
      try {
        afterFilter = 0;
        api.forEachNodeAfterFilter(function () { afterFilter++; });
      } catch (e2) {
        try { afterFilter = api.getDisplayedRowCount(); } catch (e3) {}
      }
      var page = 0, pageCount = 1, pageSz = pageSize;
      try {
        page = api.paginationGetCurrentPage();
        pageCount = Math.max(1, api.paginationGetTotalPages());
        pageSz = api.paginationGetPageSize();
      } catch (e4) {}
      var start = afterFilter === 0 ? 0 : page * pageSz + 1;
      var end = Math.min((page + 1) * pageSz, afterFilter);
      status.textContent = start + "–" + end + " of " + afterFilter +
        (afterFilter !== total ? " (filtered from " + total + ")" : "");
      prevBtn.disabled = page <= 0;
      nextBtn.disabled = page + 1 >= pageCount;
      pager.hidden = afterFilter <= pageSz && page === 0;
      var filtering = !!search.value;
      try { filtering = filtering || api.isAnyFilterPresent(); } catch (e5) {}
      clearBtn.hidden = !filtering;
      // AG Grid keeps a 150px floor for its "no rows" overlay; only keep it
      // when there is nothing to show.
      host.classList.toggle("is-empty", afterFilter === 0);
      pageSel.hidden = total <= 25;
    }
    function themeSync() {
      applyAgThemeClass(host);
    }
    if (table._delThemeObs) {
      try { table._delThemeObs.disconnect(); } catch (e) {}
    }
    table._delThemeObs = new MutationObserver(themeSync);
    table._delThemeObs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    search.addEventListener("input", function () {
      api.setGridOption("quickFilterText", search.value);
      updateStatus();
    });
    clearBtn.addEventListener("click", function () {
      search.value = "";
      api.setGridOption("quickFilterText", "");
      api.setFilterModel(null);
      updateStatus();
    });
    pageSel.addEventListener("change", function () {
      var n = parseInt(pageSel.value, 10) || 50;
      api.setGridOption("paginationPageSize", n);
      api.paginationGoToFirstPage();
      persist();
      updateStatus();
    });
    prevBtn.addEventListener("click", function () { api.paginationGoToPreviousPage(); });
    nextBtn.addEventListener("click", function () { api.paginationGoToNextPage(); });
    var prefill = table.getAttribute("data-prefill");
    if (prefill) {
      search.value = prefill;
      api.setGridOption("quickFilterText", prefill);
    }
    table._delGridApi = api;
    table._delFilteredRows = function () {
      var out = [];
      api.forEachNodeAfterFilterAndSort(function (node) {
        if (node.data && node.data._tr) out.push(node.data._tr);
      });
      return out;
    };
    table.style.display = "none";
    wrap.appendChild(table);
    updateStatus();
    return true;
  }

  function enhanceTable(table) {
    if (!table || table.getAttribute("data-enhanced-ready")) return;
    if (table.classList.contains("job-steps") || table.classList.contains("table-plain")) return;
    if (!table.tHead || !table.tBodies.length) return;
    table.setAttribute("data-enhanced-ready", "1");
    if (enhanceTableAgGrid(table)) return;
    enhanceTableVanilla(table);
  }

  document.querySelectorAll("table[data-enhanced]").forEach(enhanceTable);
  document.querySelectorAll("table.table").forEach(function (table) {
    enhanceTable(table);
  });

  // =========================================================================
  // Keyboard: "/" focuses the first quick-filter
  // =========================================================================
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) {
      return;
    }
    var filter = document.querySelector(".table-filter");
    if (!filter) return;
    e.preventDefault();
    filter.focus();
    filter.select();
  });

  // =========================================================================
  // CSV export (current filtered + sorted rows)
  // =========================================================================
  function csvEscape(val) {
    var s = (val == null ? "" : String(val)).replace(/\r?\n/g, " ").trim();
    if (/[",]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function exportTableCsv(tableId) {
    var table = document.getElementById(tableId);
    if (!table) return;
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!headRow) return;
    var headers = [];
    Array.prototype.forEach.call(headRow.cells, function (th) {
      headers.push(csvEscape((th.textContent || "").replace(/▾\s*$/, "").trim()));
    });
    var lines = [headers.join(",")];
    // Prefer the vanilla renderer's filtered set: pagination removes off-page
    // rows from the DOM, so tbody.rows alone would export one page only.
    var rows = table._delFilteredRows
      ? table._delFilteredRows()
      : (table.tBodies[0] ? Array.prototype.slice.call(table.tBodies[0].rows) : []);
    rows.forEach(function (r) {
      if (r.hidden || r.style.display === "none") return;
      var cols = [];
      Array.prototype.forEach.call(r.cells, function (td) {
        var explicit = td.getAttribute("data-sort-value");
        var text = explicit !== null && explicit !== "" ? explicit : (td.textContent || "");
        cols.push(csvEscape(text));
      });
      lines.push(cols.join(","));
    });
    var blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = (tableId || "export") + ".csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  document.querySelectorAll("[data-export-table]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      exportTableCsv(btn.getAttribute("data-export-table"));
    });
  });

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
      showToast("Copied to clipboard", "ok");
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
  // Flash messages: always dismissible by hand; only successes time out
  // =========================================================================
  function removeFlash(f) {
    f.style.transition = "opacity .4s";
    f.style.opacity = "0";
    setTimeout(function () { if (f.parentNode) f.parentNode.removeChild(f); }, 450);
  }

  document.querySelectorAll(".flash").forEach(function (f) {
    var close = document.createElement("button");
    close.type = "button";
    close.className = "flash-dismiss";
    close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "×";
    close.addEventListener("click", function () { removeFlash(f); });
    f.appendChild(close);
    // Errors stay put — they used to vanish after 4s before anyone read them.
    if (f.hasAttribute("data-autodismiss")) {
      setTimeout(function () { removeFlash(f); }, 4000);
    }
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
  // Double-submit guard for destructive forms (scan, build plan, execute, …)
  // =========================================================================
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.hasAttribute || !form.hasAttribute("data-submit-guard")) return;
    // A confirm dialog or the live-execute gate may have cancelled the submit.
    if (evt.defaultPrevented) return;
    if (form._delSubmitting) { evt.preventDefault(); return; }
    form._delSubmitting = true;
    var btn = evt.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    var busyLabel = form.getAttribute("data-submit-guard") || "Working…";
    // Deferred: disabling synchronously would drop the button's own name/value
    // from the submitted form data (the approve/exclude buttons rely on it).
    setTimeout(function () {
      btn.disabled = true;
      btn.classList.add("is-busy");
      if (btn.querySelector(".logout-label")) return;
      btn.textContent = busyLabel;
    }, 0);
  });

  // Live HTTPS probes take ~800ms; say so instead of looking dead.
  document.querySelectorAll("[data-pending-label]").forEach(function (el) {
    el.addEventListener("click", function () {
      el.setAttribute("aria-busy", "true");
      el.classList.add("is-busy");
      el.textContent = el.getAttribute("data-pending-label");
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
      if (backup) backup.value = "none";
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
  var REQUIRED_PHRASE = "y";

  function refreshExecuteGate() {
    if (!executeBtn) return;
    var isLive = modeLive && modeLive.checked;
    if (liveBox) liveBox.hidden = !isLive;
    // The confirm-phrase field only exists on plans that actually contain a
    // volume_rm step (see plan.html); plans without one have nothing to gate.
    if (isLive && phraseInput) {
      executeBtn.disabled = phraseInput.value !== REQUIRED_PHRASE;
    } else {
      executeBtn.disabled = false;
    }
    // Calm by default: a dry run is the primary action; only a live run
    // turns the button into a destructive one and says so.
    executeBtn.classList.toggle("btn-primary", !isLive);
    executeBtn.classList.toggle("btn-danger", !!isLive);
    var label = executeBtn.getAttribute(isLive ? "data-label-live" : "data-label-dry");
    if (label) executeBtn.textContent = label;
  }

  if (executeForm) {
    executeForm.querySelectorAll('input[name="mode"]').forEach(function (r) {
      r.addEventListener("change", refreshExecuteGate);
    });
    if (phraseInput) phraseInput.addEventListener("input", refreshExecuteGate);
    executeForm.addEventListener("submit", function (evt) {
      var isLive = modeLive && modeLive.checked;
      if (isLive && phraseInput && phraseInput.value !== REQUIRED_PHRASE) {
        evt.preventDefault();
        window.alert('Type "' + REQUIRED_PHRASE + '" to confirm live volume deletion.');
        return;
      }
      // A dry run changes nothing, so only a live run asks for confirmation.
      if (isLive && !window.confirm("Run this removal plan LIVE now? Irreversible steps cannot be undone.")) {
        evt.preventDefault();
      }
    });
    refreshExecuteGate();
  }

  // =========================================================================
  // View Apps: browser-local homelab gallery preferences and layout editor
  // =========================================================================
  var appGallery = document.getElementById("app-gallery");
  if (appGallery) {
    var galleryCards = Array.prototype.slice.call(appGallery.querySelectorAll(".app-launch-card"));
    var gallerySearch = document.getElementById("gallery-search");
    var gallerySearchOp = document.getElementById("gallery-search-op");
    var galleryCategory = document.getElementById("gallery-category");
    var gallerySort = document.getElementById("gallery-sort");
    var galleryWidth = document.getElementById("gallery-card-width");
    var galleryWidthValue = document.getElementById("gallery-card-width-value");
    var galleryDensity = document.getElementById("gallery-density");
    var galleryLayoutToggle = document.getElementById("gallery-layout-toggle");
    var galleryLayoutPanel = document.getElementById("gallery-layout-panel");
    var galleryShowHidden = document.getElementById("gallery-show-hidden");
    var galleryReset = document.getElementById("gallery-reset");
    var galleryEmpty = document.getElementById("gallery-empty");
    var galleryCount = document.getElementById("gallery-visible-count");
    var GALLERY_KEY = "del.appGallery.v1";
    var galleryEditing = false;
    var draggedCard = null;
    var defaultPrefs = {
      view: "grid", density: "comfortable", width: 250, sort: "category",
      favorites: {}, hidden: {}, categories: {}, order: [],
    };
    var galleryPrefs = JSON.parse(JSON.stringify(defaultPrefs));

    try {
      var storedGalleryPrefs = JSON.parse(localStorage.getItem(GALLERY_KEY) || "null");
      if (storedGalleryPrefs && typeof storedGalleryPrefs === "object") {
        Object.keys(defaultPrefs).forEach(function (key) {
          if (storedGalleryPrefs[key] !== undefined) galleryPrefs[key] = storedGalleryPrefs[key];
        });
      }
    } catch (e) {}

    function saveGalleryPrefs() {
      try { localStorage.setItem(GALLERY_KEY, JSON.stringify(galleryPrefs)); } catch (e) {}
    }

    function cardId(card) { return card.getAttribute("data-app-id") || ""; }
    function cardCategory(card) {
      return galleryPrefs.categories[cardId(card)] || card.getAttribute("data-category") || "Other";
    }
    function customOrderIndex(card) {
      var idx = galleryPrefs.order.indexOf(cardId(card));
      return idx === -1 ? 999999 : idx;
    }
    function compareCards(a, b) {
      var mode = galleryPrefs.sort;
      if (mode === "custom") return customOrderIndex(a) - customOrderIndex(b);
      if (mode === "domain") return a.getAttribute("data-domain").localeCompare(b.getAttribute("data-domain"));
      if (mode === "latency") return Number(a.getAttribute("data-latency")) - Number(b.getAttribute("data-latency"));
      return a.getAttribute("data-name").localeCompare(b.getAttribute("data-name"));
    }
    function makeGallerySection(category, cards) {
      var section = document.createElement("section");
      section.className = "gallery-section";
      section.setAttribute("data-gallery-section", category);
      var heading = document.createElement("div");
      heading.className = "gallery-section-heading";
      var title = document.createElement("h2");
      title.textContent = category;
      var count = document.createElement("span");
      count.className = "count-pill";
      count.textContent = String(cards.length);
      heading.appendChild(title);
      heading.appendChild(count);
      var grid = document.createElement("div");
      grid.className = "gallery-grid";
      cards.forEach(function (card) { grid.appendChild(card); });
      section.appendChild(heading);
      section.appendChild(grid);
      return section;
    }
    function applyCardState(card) {
      var id = cardId(card);
      var favorite = !!galleryPrefs.favorites[id];
      var hidden = !!galleryPrefs.hidden[id];
      var favoriteBtn = card.querySelector(".app-favorite");
      var hiddenInput = card.querySelector(".gallery-card-hidden");
      var categoryInput = card.querySelector(".gallery-card-category");
      var editor = card.querySelector(".app-card-editor");
      card.classList.toggle("is-favorite", favorite);
      card.classList.toggle("is-user-hidden", hidden);
      card.draggable = galleryEditing;
      if (favoriteBtn) {
        var appName = card.querySelector(".app-launch-name");
        appName = appName ? appName.textContent.trim() : "app";
        favoriteBtn.textContent = favorite ? "★" : "☆";
        favoriteBtn.setAttribute("aria-pressed", favorite ? "true" : "false");
        favoriteBtn.setAttribute(
          "aria-label",
          (favorite ? "Remove " : "Add ") + appName + (favorite ? " from favorites" : " to favorites")
        );
      }
      if (hiddenInput) hiddenInput.checked = hidden;
      if (categoryInput) categoryInput.value = cardCategory(card);
      if (editor) editor.hidden = !galleryEditing;
    }
    function renderGallery() {
      var query = (gallerySearch && gallerySearch.value || "").trim().toLowerCase();
      var categoryFilter = galleryCategory ? galleryCategory.value : "";
      var showHidden = galleryEditing && galleryShowHidden && galleryShowHidden.checked;
      var visible = galleryCards.filter(function (card) {
        applyCardState(card);
        var id = cardId(card);
        if (galleryPrefs.hidden[id] && !showHidden) return false;
        if (categoryFilter === "__favorites" && !galleryPrefs.favorites[id]) return false;
        if (categoryFilter && categoryFilter !== "__favorites" && cardCategory(card) !== categoryFilter) return false;
        if (query) {
          var text = (card.getAttribute("data-name") + " " + card.getAttribute("data-domain") + " " + cardCategory(card)).toLowerCase();
          var op = gallerySearchOp ? gallerySearchOp.value : "contains";
          var hit = text.indexOf(query) !== -1;
          if (op === "notContains") { if (hit) return false; }
          else if (op === "equals") { if (text.trim() !== query) return false; }
          else if (op === "startsWith") {
            var name = (card.getAttribute("data-name") || "").toLowerCase();
            var domain = (card.getAttribute("data-domain") || "").toLowerCase();
            if (name.indexOf(query) !== 0 && domain.indexOf(query) !== 0) return false;
          } else if (!hit) return false;
        }
        return true;
      });
      visible.sort(compareCards);

      var groups = {};
      visible.forEach(function (card) {
        var category = categoryFilter === "__favorites" ? "Favorites" : cardCategory(card);
        (groups[category] = groups[category] || []).push(card);
      });
      var preferred = [
        "Favorites", "AI & Automation", "Media & Streaming", "Notes & Knowledge",
        "Productivity", "Files & Data", "Developer Tools", "Infrastructure",
        "Security & Identity", "Business & Finance", "Utilities", "Other",
      ];
      var groupNames = Object.keys(groups).sort(function (a, b) {
        var ai = preferred.indexOf(a); var bi = preferred.indexOf(b);
        if (ai === -1) ai = 999; if (bi === -1) bi = 999;
        return ai === bi ? a.localeCompare(b) : ai - bi;
      });
      appGallery.innerHTML = "";
      groupNames.forEach(function (name) { appGallery.appendChild(makeGallerySection(name, groups[name])); });
      appGallery.setAttribute("data-view", galleryPrefs.view);
      appGallery.setAttribute("data-density", galleryPrefs.density);
      appGallery.style.setProperty("--gallery-card-min", galleryPrefs.width + "px");
      if (galleryCount) galleryCount.textContent = String(visible.length);
      if (galleryEmpty) galleryEmpty.hidden = visible.length !== 0;
      document.querySelectorAll("[data-gallery-view]").forEach(function (button) {
        var active = button.getAttribute("data-gallery-view") === galleryPrefs.view;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
    }

    galleryCards.forEach(function (card) {
      var icon = card.querySelector(".app-icon");
      var favorite = card.querySelector(".app-favorite");
      var categoryInput = card.querySelector(".gallery-card-category");
      var hiddenInput = card.querySelector(".gallery-card-hidden");
      if (icon) icon.addEventListener("error", function () { card.classList.add("icon-failed"); });
      if (favorite) favorite.addEventListener("click", function () {
        var id = cardId(card);
        galleryPrefs.favorites[id] = !galleryPrefs.favorites[id];
        if (!galleryPrefs.favorites[id]) delete galleryPrefs.favorites[id];
        saveGalleryPrefs(); renderGallery();
      });
      if (categoryInput) categoryInput.addEventListener("change", function () {
        galleryPrefs.categories[cardId(card)] = categoryInput.value;
        saveGalleryPrefs(); renderGallery();
      });
      if (hiddenInput) hiddenInput.addEventListener("change", function () {
        if (hiddenInput.checked) galleryPrefs.hidden[cardId(card)] = true;
        else delete galleryPrefs.hidden[cardId(card)];
        saveGalleryPrefs(); renderGallery();
      });
      card.addEventListener("dragstart", function (event) {
        if (!galleryEditing) return;
        draggedCard = card;
        card.classList.add("is-dragging");
        // Firefox refuses to start a drag unless dataTransfer carries something.
        if (event.dataTransfer) {
          try {
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", cardId(card));
          } catch (e) {}
        }
      });
      card.addEventListener("dragend", function () {
        card.classList.remove("is-dragging");
        draggedCard = null;
        galleryPrefs.order = Array.prototype.map.call(appGallery.querySelectorAll(".app-launch-card"), cardId);
        galleryPrefs.sort = "custom";
        if (gallerySort) gallerySort.value = "custom";
        saveGalleryPrefs(); renderGallery();
      });
    });
    appGallery.addEventListener("dragover", function (event) {
      if (!draggedCard) return;
      event.preventDefault();
      var target = event.target.closest(".app-launch-card");
      if (!target || target === draggedCard || target.parentNode !== draggedCard.parentNode) return;
      var rect = target.getBoundingClientRect();
      var before = event.clientY < rect.top + rect.height / 2;
      target.parentNode.insertBefore(draggedCard, before ? target : target.nextSibling);
    });
    if (gallerySearch) gallerySearch.addEventListener("input", renderGallery);
    if (gallerySearchOp) gallerySearchOp.addEventListener("change", renderGallery);
    if (galleryCategory) galleryCategory.addEventListener("change", renderGallery);
    if (gallerySort) gallerySort.addEventListener("change", function () {
      galleryPrefs.sort = gallerySort.value; saveGalleryPrefs(); renderGallery();
    });
    document.querySelectorAll("[data-gallery-view]").forEach(function (button) {
      button.addEventListener("click", function () {
        galleryPrefs.view = button.getAttribute("data-gallery-view");
        saveGalleryPrefs(); renderGallery();
      });
    });
    if (galleryWidth) galleryWidth.addEventListener("input", function () {
      galleryPrefs.width = Number(galleryWidth.value);
      if (galleryWidthValue) galleryWidthValue.textContent = galleryPrefs.width + "px";
      saveGalleryPrefs(); renderGallery();
    });
    if (galleryDensity) galleryDensity.addEventListener("change", function () {
      galleryPrefs.density = galleryDensity.value; saveGalleryPrefs(); renderGallery();
    });
    if (galleryLayoutToggle) galleryLayoutToggle.addEventListener("click", function () {
      galleryEditing = !galleryEditing;
      if (galleryLayoutPanel) galleryLayoutPanel.hidden = !galleryEditing;
      galleryLayoutToggle.setAttribute("aria-expanded", galleryEditing ? "true" : "false");
      galleryLayoutToggle.textContent = galleryEditing ? "Done" : "Customize";
      appGallery.classList.toggle("is-customizing", galleryEditing);
      renderGallery();
    });
    if (galleryShowHidden) galleryShowHidden.addEventListener("change", renderGallery);
    if (galleryReset) galleryReset.addEventListener("click", function () {
      if (!window.confirm("Reset this browser’s app gallery layout and card preferences?")) return;
      galleryPrefs = JSON.parse(JSON.stringify(defaultPrefs));
      saveGalleryPrefs();
      if (gallerySearch) gallerySearch.value = "";
      if (galleryCategory) galleryCategory.value = "";
      if (gallerySort) gallerySort.value = galleryPrefs.sort;
      if (galleryWidth) galleryWidth.value = String(galleryPrefs.width);
      if (galleryWidthValue) galleryWidthValue.textContent = galleryPrefs.width + "px";
      if (galleryDensity) galleryDensity.value = galleryPrefs.density;
      if (galleryShowHidden) galleryShowHidden.checked = false;
      renderGallery();
    });
    if (gallerySort) gallerySort.value = galleryPrefs.sort;
    if (galleryWidth) galleryWidth.value = String(galleryPrefs.width);
    if (galleryWidthValue) galleryWidthValue.textContent = galleryPrefs.width + "px";
    if (galleryDensity) galleryDensity.value = galleryPrefs.density;
    renderGallery();
  }

  // =========================================================================
  // Focus management for the two modal surfaces (nav drawer, glossary sheet)
  // =========================================================================
  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function focusableIn(container) {
    if (!container) return [];
    return Array.prototype.filter.call(container.querySelectorAll(FOCUSABLE), function (el) {
      return el.offsetWidth || el.offsetHeight || el.getClientRects().length;
    });
  }

  // Looked up live: the nav drawer can close before the sheet element is bound.
  function glossarySheetOpen() {
    var sheet = document.getElementById("glossary-sheet");
    return !!(sheet && !sheet.hidden);
  }

  // Returns an object that moves focus in, keeps Tab inside, and restores focus
  // to whatever was focused before the surface opened.
  function makeFocusTrap(getContainer) {
    var lastFocused = null;
    var active = false;

    function onKeydown(e) {
      if (e.key !== "Tab" || !active) return;
      var items = focusableIn(getContainer());
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    return {
      activate: function () {
        if (active) return;
        active = true;
        lastFocused = document.activeElement;
        document.addEventListener("keydown", onKeydown, true);
        setTimeout(function () {
          var items = focusableIn(getContainer());
          if (items.length) items[0].focus();
        }, 50);
      },
      release: function (fallback) {
        if (!active) return;
        active = false;
        document.removeEventListener("keydown", onKeydown, true);
        var target = fallback || lastFocused;
        lastFocused = null;
        if (target && target.focus && document.contains(target)) target.focus();
      },
    };
  }

  // =========================================================================
  // Shell: desktop collapse rail  vs  mobile flyout (never mixed)
  // =========================================================================
  var layout = document.getElementById("app-layout");
  var sidebar = document.getElementById("sidebar");
  var navToggle = document.getElementById("nav-toggle");
  var sidebarCollapse = document.getElementById("sidebar-collapse");
  var sidebarResizer = document.getElementById("sidebar-resizer");
  var navBackdrop = document.getElementById("nav-backdrop");
  var navDrawer = document.getElementById("nav-drawer");
  var SIDEBAR_KEY = "del.sidebarCollapsed";
  var SIDEBAR_WIDTH_KEY = "del.sidebarWidth";

  function clampPanelWidth(value, min, max) {
    return Math.max(min, Math.min(max, Math.round(value)));
  }

  function makePanelResizable(handle, panel, cssVariable, storageKey, min, max, initial, direction) {
    if (!handle || !panel || !layout) return;

    function apply(value, remember) {
      var width = clampPanelWidth(value, min, max);
      layout.style.setProperty(cssVariable, width + "px");
      handle.setAttribute("aria-valuenow", String(width));
      if (remember !== false) {
        try { localStorage.setItem(storageKey, String(width)); } catch (e) {}
      }
      return width;
    }

    try {
      var stored = parseInt(localStorage.getItem(storageKey), 10);
      apply(Number.isFinite(stored) ? stored : initial, false);
    } catch (e) { apply(initial, false); }

    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      event.preventDefault();
      var startX = event.clientX;
      var startWidth = panel.getBoundingClientRect().width;
      handle.classList.add("is-dragging");
      document.body.classList.add("is-panel-resizing");
      handle.setPointerCapture(event.pointerId);

      function move(moveEvent) {
        apply(startWidth + ((moveEvent.clientX - startX) * direction));
      }
      function finish() {
        handle.classList.remove("is-dragging");
        document.body.classList.remove("is-panel-resizing");
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", finish);
        handle.removeEventListener("pointercancel", finish);
      }
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", finish);
      handle.addEventListener("pointercancel", finish);
    });

    handle.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      var current = panel.getBoundingClientRect().width;
      var step = event.shiftKey ? 32 : 12;
      var physicalDelta = event.key === "ArrowRight" ? step : -step;
      apply(current + (physicalDelta * direction));
    });

    handle.addEventListener("dblclick", function () {
      apply(initial);
    });
  }

  makePanelResizable(
    sidebarResizer, sidebar, "--sidebar-width", SIDEBAR_WIDTH_KEY,
    184, 320, 212, 1
  );

  function isMobileNav() {
    return matchesMobile();
  }

  var navTrap = makeFocusTrap(function () { return navDrawer; });

  function setSidebarCollapsed(collapsed) {
    if (!layout) return;
    // Collapse is desktop-only. On mobile always clear the class so labels
    // never stay at font-size 0 / clipped when the flyout opens.
    if (isMobileNav()) {
      layout.classList.remove("sidebar-collapsed");
      closeMobileNav();
      return;
    }
    closeMobileNav(); // never leave mobile drawer/backdrop on desktop
    layout.classList.toggle("sidebar-collapsed", !!collapsed);
    try { localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); } catch (e) {}
    if (sidebarCollapse) {
      // The chevron icon is rotated by CSS (.sidebar-collapsed); only labels change.
      sidebarCollapse.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      sidebarCollapse.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    }
  }

  function closeMobileNav() {
    var wasOpen = !!(sidebar && sidebar.classList.contains("open"));
    if (sidebar) sidebar.classList.remove("open");
    if (navToggle) {
      navToggle.setAttribute("aria-expanded", "false");
      navToggle.setAttribute("aria-label", "Open menu");
    }
    if (navBackdrop) navBackdrop.hidden = true;
    // Always clear: syncShellMode() calls this on the desktop branch, where
    // isMobileNav() is already false — guarding here left the page unscrollable
    // after opening the menu and widening past the breakpoint.
    if (!glossarySheetOpen()) document.body.style.overflow = "";
    if (wasOpen) navTrap.release(navToggle);
  }

  function openMobileNav() {
    if (!sidebar || !isMobileNav()) return;
    // Ensure desktop-collapse styles aren't fighting the flyout
    if (layout) layout.classList.remove("sidebar-collapsed");
    sidebar.classList.add("open");
    if (navToggle) {
      navToggle.setAttribute("aria-expanded", "true");
      navToggle.setAttribute("aria-label", "Close menu");
    }
    if (navBackdrop) navBackdrop.hidden = false;
    document.body.style.overflow = "hidden";
    navTrap.activate();
  }

  function syncShellMode() {
    if (isMobileNav()) {
      if (layout) layout.classList.remove("sidebar-collapsed");
      // keep drawer closed on mode switch unless already open intentionally
    } else {
      closeMobileNav();
      try {
        setSidebarCollapsed(localStorage.getItem(SIDEBAR_KEY) === "1");
      } catch (e) {
        setSidebarCollapsed(false);
      }
    }
  }

  // Initial: desktop may restore collapse; mobile never collapses
  syncShellMode();

  if (sidebarCollapse) {
    sidebarCollapse.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (isMobileNav()) return;
      setSidebarCollapsed(!layout.classList.contains("sidebar-collapsed"));
    });
  }

  if (navToggle) {
    navToggle.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (!isMobileNav()) {
        // Hamburger must not run on desktop (should be hidden); if it does, expand rail.
        setSidebarCollapsed(false);
        return;
      }
      if (sidebar && sidebar.classList.contains("open")) closeMobileNav();
      else openMobileNav();
    });
  }
  if (navBackdrop) {
    navBackdrop.addEventListener("click", closeMobileNav);
  }
  // Close flyout after navigating (same-tab links only)
  if (sidebar) {
    sidebar.querySelectorAll(".nav-links a").forEach(function (a) {
      a.addEventListener("click", function () {
        if (isMobileNav() && a.target !== "_blank") closeMobileNav();
      });
    });
  }
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(syncShellMode, 100);
  });

  // =========================================================================
  // Glossary: context filter (page + resource tab) + mobile sheet
  // =========================================================================
  var GLOSSARY_COLLAPSE_KEY = "del.glossaryCollapsed";
  var glossaryCollapseBtn = document.getElementById("glossary-collapse");
  var glossaryRail = document.getElementById("glossary-rail");
  var railResizer = document.getElementById("rail-resizer");
  makePanelResizable(
    railResizer, glossaryRail, "--rail-width", "del.rightRailWidth",
    280, 520, 300, -1
  );
  function setGlossaryCollapsed(collapsed, remember) {
    if (!layout) return;
    if (window.matchMedia && window.matchMedia("(max-width: 1279px)").matches) {
      layout.classList.remove("glossary-collapsed");
      return;
    }
    layout.classList.toggle("glossary-collapsed", !!collapsed);
    if (remember !== false) {
      try { localStorage.setItem(GLOSSARY_COLLAPSE_KEY, collapsed ? "1" : "0"); } catch (e) {}
    }
    if (glossaryCollapseBtn) {
      glossaryCollapseBtn.setAttribute("aria-label", collapsed ? "Show help panel" : "Hide help panel");
      glossaryCollapseBtn.title = collapsed ? "Show help panel" : "Hide help panel";
    }
  }
  // No saved choice yet: keep the rail open only on wide screens (>= 1600px)
  // so tables get the width on a typical laptop; the choice is not stored.
  try {
    var savedRail = localStorage.getItem(GLOSSARY_COLLAPSE_KEY);
    if (savedRail === null) setGlossaryCollapsed(window.innerWidth < 1600, false);
    else setGlossaryCollapsed(savedRail === "1", false);
  } catch (e) {}
  if (glossaryCollapseBtn) {
    glossaryCollapseBtn.addEventListener("click", function () {
      setGlossaryCollapsed(!(layout && layout.classList.contains("glossary-collapsed")));
    });
  }
  document.querySelectorAll("[data-rail-expand]").forEach(function (btn) {
    btn.addEventListener("click", function () { setGlossaryCollapsed(false); });
  });

  var RAIL_TAB_KEY = "del.rightRailTab";
  function setRailTab(tab) {
    tab = tab === "ask" ? "ask" : "help";
    document.querySelectorAll(".rail-tab").forEach(function (btn) {
      var on = btn.getAttribute("data-rail-tab") === tab;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll("[data-rail-panel]").forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-rail-panel") !== tab;
    });
    if (layout) layout.classList.toggle("ask-open", tab === "ask");
    try { localStorage.setItem(RAIL_TAB_KEY, tab); } catch (e) {}
    if (tab === "ask") setGlossaryCollapsed(false);
  }
  document.querySelectorAll(".rail-tab").forEach(function (btn) {
    btn.addEventListener("click", function () { setRailTab(btn.getAttribute("data-rail-tab")); });
  });
  try {
    var savedTab = localStorage.getItem(RAIL_TAB_KEY);
    if (savedTab === "ask" && document.getElementById("rail-panel-ask")) setRailTab("ask");
  } catch (e) {}
  var askFab = document.getElementById("assistant-fab");
  if (askFab) {
    askFab.addEventListener("click", function () {
      setRailTab("ask");
      if (layout) layout.classList.add("ask-open");
    });
  }

  document.addEventListener("click", function (evt) {
    var link = evt.target && evt.target.closest && evt.target.closest("[data-ask-scope], [data-ask-target]");
    if (!link) return;
    if (!document.getElementById("assistant-dock")) return;
    evt.preventDefault();
    var scope = link.getAttribute("data-ask-scope") || link.getAttribute("data-scope") || "general";
    var target = link.getAttribute("data-ask-target") || "";
    var rtype = link.getAttribute("data-resource-type") || "";
    if (!rtype && scope === "resource" && target.indexOf(":") !== -1) rtype = target.split(":")[0];
    if (!rtype && scope === "resource_type") rtype = target;
    var draft = link.getAttribute("data-ask-draft") || "";
    if (!draft && target) draft = "What should I know about " + target + "? Name every owner, shared, and data_loss_risk.";
    else if (!draft) draft = "What should I know about this screen?";
    if (window.DEL && window.DEL.assistant && typeof window.DEL.assistant.applyAsk === "function") {
      window.DEL.assistant.applyAsk(scope, target, rtype, draft);
    }
    setRailTab("ask");
  });

  var GLOSSARY_LABELS = {
    general: "General",
    "view-apps": "View Apps",
    apps: "Applications",
    "app-detail": "Application detail",
    orphans: "Orphans",
    assistant: "Assistant",
    jobs: "Jobs",
    "job-detail": "Job detail",
    resources: "Resources",
    "resources-container": "Resources · Containers",
    "resources-image": "Resources · Images",
    "resources-volume": "Resources · Volumes",
    "resources-network": "Resources · Networks",
    "resources-nginx_site": "Resources · Nginx",
    "resources-directory": "Resources · Directories",
    "resources-git_repo": "Resources · Git repos",
    "resources-systemd_unit": "Resources · systemd",
    "resources-systemd_timer": "Resources · Timers",
    "resources-compose_project": "Resources · Compose",
    "resources-cron_entry": "Resources · Cron",
    "resources-process": "Resources · Processes",
    "resources-port": "Resources · Ports",
    "resources-env_file": "Resources · Env files",
    "resources-bind_mount": "Resources · Bind mounts",
    "resources-tmux_session": "Resources · tmux",
  };

  function glossaryCtxFromPath(pathname) {
    pathname = pathname || window.location.pathname;
    if (pathname === "/") return "general";
    if (pathname.indexOf("/view-apps") === 0) return "view-apps";
    if (pathname === "/apps") return "apps";
    if (pathname.indexOf("/apps/") === 0 && pathname.indexOf("/plan") !== -1) return "jobs";
    if (pathname.indexOf("/apps/") === 0) return "app-detail";
    if (pathname.indexOf("/resources/") === 0) {
      var parts = pathname.split("/").filter(Boolean);
      return parts.length >= 2 ? "resources-" + parts[1] : "resources";
    }
    if (pathname.indexOf("/resources") === 0) return "resources";
    if (pathname.indexOf("/orphans") === 0) return "orphans";
    if (pathname.indexOf("/assistant") === 0) return "assistant";
    if (pathname.indexOf("/jobs/") === 0) return "job-detail";
    if (pathname.indexOf("/jobs") === 0) return "jobs";
    if (pathname.indexOf("/settings") === 0) return "general";
    return "general";
  }

  function applyGlossaryContext(ctx) {
    ctx = ctx || glossaryCtxFromPath() || "general";
    document.body.setAttribute("data-glossary", ctx);
    var label = GLOSSARY_LABELS[ctx] || ctx;
    document.querySelectorAll(".glossary-context-label").forEach(function (el) {
      el.textContent = label;
    });
    document.querySelectorAll(".glossary-section[data-g]").forEach(function (sec) {
      var keys = (sec.getAttribute("data-g") || "").split(/\s+/);
      var show = keys.indexOf(ctx) !== -1 || keys.indexOf("general") !== -1 && ctx === "general";
      // Also show shared "resources" base sections for any resources-* ctx
      if (!show && ctx.indexOf("resources-") === 0) {
        show = keys.indexOf("resources") !== -1 || keys.indexOf(ctx) !== -1;
      }
      // Orphans page: only orphans (+ times via its data-g list) — avoid dumping apps
      if (ctx === "orphans") {
        show = keys.indexOf("orphans") !== -1;
      }
      if (ctx === "apps" || ctx === "app-detail") {
        show = keys.indexOf(ctx) !== -1 || keys.indexOf("apps") !== -1;
      }
      sec.classList.toggle("is-hidden", !show);
      if (show) sec.removeAttribute("hidden");
      else sec.setAttribute("hidden", "");
    });
  }

  // Initial paint + keep in sync when resource tab links are activated
  applyGlossaryContext(document.body.getAttribute("data-glossary") || glossaryCtxFromPath());

  document.querySelectorAll(".subnav a[href*='/resources/'], .tabbar a[href*='/resources/']").forEach(function (a) {
    a.addEventListener("click", function () {
      try {
        var u = new URL(a.href, window.location.origin);
        applyGlossaryContext(glossaryCtxFromPath(u.pathname));
      } catch (e) {}
    });
  });

  // SPA-ish: if user uses back/forward after full loads, re-apply
  window.addEventListener("popstate", function () {
    applyGlossaryContext(glossaryCtxFromPath());
  });

  var glossaryFab = document.getElementById("glossary-fab");
  var glossarySheet = document.getElementById("glossary-sheet");
  var glossaryClose = document.getElementById("glossary-sheet-close");
  var glossaryBackdrop = document.getElementById("glossary-sheet-backdrop");

  var glossaryTrap = makeFocusTrap(function () {
    return glossarySheet ? glossarySheet.querySelector(".glossary-sheet-panel") : null;
  });

  function openGlossary() {
    if (!glossarySheet) return;
    applyGlossaryContext(document.body.getAttribute("data-glossary") || glossaryCtxFromPath());
    glossarySheet.hidden = false;
    if (glossaryFab) glossaryFab.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
    // aria-modal="true" is a promise: move focus in and keep Tab inside.
    glossaryTrap.activate();
  }
  function closeGlossary() {
    if (!glossarySheet || glossarySheet.hidden) return;
    glossarySheet.hidden = true;
    if (glossaryFab) glossaryFab.setAttribute("aria-expanded", "false");
    if (!sidebar || !sidebar.classList.contains("open")) {
      document.body.style.overflow = "";
    }
    glossaryTrap.release(glossaryFab);
  }
  document.querySelectorAll("[data-glossary-open]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (glossaryFab && glossaryFab.offsetParent !== null) {
        openGlossary();
      } else {
        var rail = document.getElementById("glossary-rail");
        if (rail) rail.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  if (glossaryFab) glossaryFab.addEventListener("click", openGlossary);
  if (glossaryClose) glossaryClose.addEventListener("click", closeGlossary);
  if (glossaryBackdrop) glossaryBackdrop.addEventListener("click", closeGlossary);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      closeGlossary();
      closeMobileNav();
    }
  });

  // =========================================================================
  // Job status polling + auto-scroll
  // =========================================================================
  var outputBox = document.getElementById("job-output");
  if (outputBox) {
    var jobId = outputBox.getAttribute("data-job-id");
    var statusEl = document.getElementById("job-status");
    var scrollToggle = document.getElementById("autoscroll-toggle");
    var progressFill = document.getElementById("job-progress-fill");
    var progressTrack = document.getElementById("job-progress");
    var progressLabel = document.getElementById("job-progress-label");
    var currentStepEl = document.getElementById("job-current-step");
    var noStepsMsg = document.getElementById("job-no-steps");
    var TERMINAL_STATES = ["done", "failed", "success", "error", "refused"];
    var toastedDone = false;

    // Build (or find) the stage panel a step belongs to, mirroring the
    // server-rendered markup in job_detail.html. The shape is decided from
    // the polled payload every time, never from whatever happens to be in
    // the DOM — a job with zero steps at page load must still grow full
    // per-stage tables once steps start arriving.
    function stageTableFor(stage) {
      var table = outputBox.querySelector('table[data-stage="' + stage.replace(/"/g, "") + '"]');
      if (table) return table;
      if (noStepsMsg && noStepsMsg.parentNode) {
        noStepsMsg.parentNode.removeChild(noStepsMsg);
        noStepsMsg = null;
      }
      var section = document.createElement("section");
      section.className = "panel";
      section.innerHTML =
        "<h2>" + escapeHtml(stage) + ' <span class="count-pill">0</span></h2>' +
        '<div class="table-scroll"><table class="table job-steps" data-stage="' + escapeHtml(stage) + '">' +
        '<caption class="sr-only">Steps in the ' + escapeHtml(stage) + " stage</caption>" +
        '<thead><tr><th scope="col">#</th><th scope="col">Operation</th><th scope="col">State</th>' +
        '<th scope="col">Exit</th><th scope="col">Duration</th><th scope="col">Output</th></tr></thead>' +
        "<tbody></tbody></table></div>";
      outputBox.appendChild(section);
      return section.querySelector("table");
    }

    function appendStepRow(step) {
      var table = stageTableFor(step.stage || "steps");
      var row = document.createElement("tr");
      row.setAttribute("data-step-seq", step.seq);
      row.innerHTML =
        "<td>" + escapeHtml(step.seq) + "</td>" +
        '<td class="mono">' + escapeHtml(step.operation || "") + "</td>" +
        '<td class="step-state"></td>' +
        '<td class="step-exit">—</td>' +
        '<td class="step-duration">—</td>' +
        '<td class="step-output">—</td>';
      table.tBodies[0].appendChild(row);
      var section = table.closest("section");
      var pill = section ? section.querySelector(".count-pill") : null;
      if (pill) pill.textContent = String(table.tBodies[0].rows.length);
      return row;
    }

    function applyStatus(data) {
      if (!data) return;
      if (statusEl && data.status) {
        statusEl.textContent = data.status;
        statusEl.className = "badge status-" + data.status +
          (TERMINAL_STATES.indexOf(data.status) === -1 ? " is-live" : "");
      }
      (data.steps || []).forEach(function (step) {
        var row = outputBox.querySelector('tr[data-step-seq="' + step.seq + '"]');
        if (!row) row = appendStepRow(step);
        if (!row) return;
        var stateCell = row.querySelector(".step-state");
        if (stateCell) {
          stateCell.innerHTML =
            '<span class="badge status-' + escapeHtml(step.state) +
            (step.state === "running" ? " is-live" : "") + '">' + escapeHtml(step.state) + "</span>";
        }
        var exitCell = row.querySelector(".step-exit");
        if (exitCell) {
          exitCell.textContent = step.exit_code === null || step.exit_code === undefined ? "—" : String(step.exit_code);
        }
        var durationCell = row.querySelector(".step-duration");
        if (durationCell) durationCell.textContent = step.duration || "—";
        var outputCell = row.querySelector(".step-output");
        if (outputCell) {
          if (step.output_sanitized) {
            outputCell.innerHTML =
              "<details><summary>output</summary><pre class=\"output\">" +
              escapeHtml(step.output_sanitized) + "</pre></details>";
          } else {
            outputCell.textContent = "—";
          }
        }
      });
      if (progressFill && data.progress) {
        var pct = data.progress.pct || 0;
        progressFill.style.width = pct + "%";
        if (progressTrack) progressTrack.setAttribute("aria-valuenow", String(pct));
        if (progressLabel) {
          progressLabel.textContent = data.progress.done + " of " + data.progress.total + " steps done (" + pct + "%)";
        }
      }
      if (currentStepEl) {
        currentStepEl.textContent = data.current_step
          ? "Running: " + (data.current_step.stage || "") + " · " + (data.current_step.operation || "")
          : "";
      }
      if (scrollToggle && scrollToggle.checked && outputBox) {
        outputBox.scrollTop = outputBox.scrollHeight;
      }
      if (data.status && TERMINAL_STATES.indexOf(data.status) !== -1 && !toastedDone) {
        toastedDone = true;
        showToast("Job " + data.status, data.status === "done" || data.status === "success" ? "ok" : "error");
      }
    }

    // Exponential backoff 2s -> 10s; pause entirely while the tab is hidden
    // and resume (with an immediate poll) when it becomes visible again.
    var pollDelay = 2000;
    var pollTimer = null;
    var stopped = false;

    function scheduleNext() {
      if (stopped || document.hidden) return;
      pollTimer = setTimeout(poll, pollDelay);
      pollDelay = Math.min(pollDelay * 1.5, 10000);
    }

    function poll() {
      pollTimer = null;
      fetch("/jobs/" + jobId + "/status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          applyStatus(data);
          if (!data || TERMINAL_STATES.indexOf(data.status) === -1) {
            scheduleNext();
          } else {
            stopped = true;
          }
        })
        .catch(function () { scheduleNext(); });
    }

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !stopped && !pollTimer) {
        pollDelay = 2000;
        poll();
      }
      if (document.hidden && pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
      }
    });

    pollTimer = setTimeout(poll, pollDelay);
  }

  // =========================================================================
  // Dashboard: live scan strip — poll /scan/status while a scan runs, disable
  // "Run scan now" meanwhile, and reload once when it flips back to idle.
  // =========================================================================
  var scanStrip = document.getElementById("scan-strip");
  if (scanStrip) {
    var scanLive = document.getElementById("scan-strip-live");
    var scanElapsed = document.getElementById("scan-strip-elapsed");
    var runScanBtn = document.getElementById("run-scan-btn");
    var scanStartedAt = null;
    var scanWasRunning = false;
    var scanPollTimer = null;

    function formatElapsed(ms) {
      var s = Math.max(0, Math.round(ms / 1000));
      if (s < 60) return s + "s";
      return Math.floor(s / 60) + "m " + (s % 60) + "s";
    }

    function tickElapsed() {
      if (!scanStartedAt || !scanElapsed) return;
      scanElapsed.textContent = "scanning… " + formatElapsed(Date.now() - scanStartedAt);
    }

    function pollScanStatus() {
      fetch("/scan/status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data) return;
          var running = !!data.running;
          if (running) {
            if (!scanStartedAt) {
              scanStartedAt = data.started ? Date.parse(data.started) : Date.now();
              if (isNaN(scanStartedAt)) scanStartedAt = Date.now();
            }
            if (scanLive) scanLive.hidden = false;
            if (runScanBtn) runScanBtn.disabled = true;
            tickElapsed();
            scanWasRunning = true;
            scanPollTimer = setTimeout(pollScanStatus, 3000);
          } else {
            if (scanLive) scanLive.hidden = true;
            if (runScanBtn) runScanBtn.disabled = false;
            if (scanWasRunning) {
              // Scan just finished: reload once to pick up fresh stats/tables.
              window.location.reload();
              return;
            }
          }
        })
        .catch(function () {
          scanPollTimer = setTimeout(pollScanStatus, 3000);
        });
    }
    pollScanStatus();

    var scanForm = document.querySelector(".scan-run-form");
    if (scanForm) {
      scanForm.addEventListener("submit", function () {
        scanWasRunning = true;
        if (runScanBtn) runScanBtn.disabled = true;
        setTimeout(pollScanStatus, 1000);
      });
    }
  }

  // =========================================================================
  // DEL.tabs: accessible tablist (roving tabindex, arrow keys, aria-selected)
  // for any `.tabs` block: a `.tablist` of `[role=tab]` buttons plus matching
  // `.tabpanel` elements, wired by aria-controls/aria-labelledby. Deep-links
  // via the URL hash when a tab's id matches window.location.hash.
  // =========================================================================
  function initTabs(root) {
    var tablist = root.querySelector('[role="tablist"]');
    if (!tablist) return;
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"]'));
    if (!tabs.length) return;

    function panelFor(tab) {
      var id = tab.getAttribute("aria-controls");
      return id ? document.getElementById(id) : null;
    }

    function select(tab, focus) {
      tabs.forEach(function (t) {
        var selected = t === tab;
        t.setAttribute("aria-selected", selected ? "true" : "false");
        t.tabIndex = selected ? 0 : -1;
        var panel = panelFor(t);
        if (panel) panel.hidden = !selected;
      });
      if (focus) tab.focus();
      if (tab.id) {
        history.replaceState(null, "", "#" + tab.id);
      }
    }

    tabs.forEach(function (tab, idx) {
      tab.addEventListener("click", function () { select(tab, false); });
      tab.addEventListener("keydown", function (e) {
        var next = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") next = tabs[(idx + 1) % tabs.length];
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = tabs[(idx - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") next = tabs[0];
        else if (e.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          e.preventDefault();
          select(next, true);
        }
      });
    });

    function tabForHash() {
      if (!window.location.hash) return null;
      var hashId = window.location.hash.slice(1);
      return tabs.filter(function (t) { return t.id === hashId; })[0] || null;
    }
    select(tabForHash() || tabs[0], false);
    // In-page links like <a href="#tab-docker"> switch tabs too.
    window.addEventListener("hashchange", function () {
      var match = tabForHash();
      if (match) select(match, false);
    });
  }

  window.DEL.tabs = { init: initTabs };
  document.querySelectorAll(".tabs").forEach(initTabs);

  // =========================================================================
  // DEL.palette: Ctrl/Cmd+K command palette. Pages come from the sidebar nav
  // already in the DOM; apps are fetched once from /palette.json (best-effort
  // — the dialog still works with pages-only if that endpoint is absent).
  // =========================================================================
  var cmdkDialog = document.getElementById("cmdk");
  if (cmdkDialog && typeof cmdkDialog.showModal === "function") {
    var cmdkInput = document.getElementById("cmdk-input");
    var cmdkResults = document.getElementById("cmdk-results");
    var cmdkEmpty = document.getElementById("cmdk-empty");
    var cmdkOpenBtn = document.getElementById("cmdk-open");
    var cmdkTrap = makeFocusTrap(function () { return cmdkDialog; });
    var cmdkPages = Array.prototype.map.call(
      document.querySelectorAll(".nav-links a"),
      function (a) { return { title: (a.textContent || "").trim(), url: a.getAttribute("href") }; }
    );
    var cmdkApps = null; // null = not yet fetched
    var cmdkSelected = 0;
    var cmdkItems = [];

    function loadApps() {
      if (cmdkApps !== null) return Promise.resolve(cmdkApps);
      return fetch("/palette.json", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { apps: [] }; })
        .then(function (data) { cmdkApps = (data && data.apps) || []; return cmdkApps; })
        .catch(function () { cmdkApps = []; return cmdkApps; });
    }

    function fuzzyMatch(text, query) {
      text = (text || "").toLowerCase();
      return text.indexOf(query) !== -1;
    }

    function renderResults(query) {
      query = (query || "").trim().toLowerCase();
      var pageMatches = cmdkPages.filter(function (p) {
        return !query || fuzzyMatch(p.title, query) || fuzzyMatch(p.url, query);
      });
      var appList = cmdkApps || [];
      var appMatches = appList.filter(function (a) {
        return !query || fuzzyMatch(a.name, query) || fuzzyMatch(a.slug, query) ||
          fuzzyMatch((a.domains || []).join(" "), query);
      });

      cmdkResults.innerHTML = "";
      cmdkItems = [];

      function addGroup(label, items, render) {
        if (!items.length) return;
        var groupLabel = document.createElement("div");
        groupLabel.className = "cmdk-group-label";
        groupLabel.textContent = label;
        cmdkResults.appendChild(groupLabel);
        items.forEach(function (item) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "cmdk-item";
          btn.setAttribute("role", "option");
          btn.innerHTML = render(item);
          btn.addEventListener("click", function () { window.location.href = item.url; });
          cmdkResults.appendChild(btn);
          cmdkItems.push(btn);
        });
      }

      addGroup("Pages", pageMatches, function (p) {
        return escapeHtml(p.title);
      });
      addGroup("Apps", appMatches, function (a) {
        return escapeHtml(a.name) + '<span class="cmdk-meta">' + escapeHtml(a.status || "") + "</span>";
      });

      cmdkEmpty.hidden = cmdkItems.length !== 0;
      cmdkSelected = 0;
      highlightSelected();
    }

    function highlightSelected() {
      cmdkItems.forEach(function (el, i) {
        el.setAttribute("aria-selected", i === cmdkSelected ? "true" : "false");
      });
      if (cmdkItems[cmdkSelected]) {
        cmdkItems[cmdkSelected].scrollIntoView({ block: "nearest" });
      }
    }

    function openPalette() {
      renderResults("");
      cmdkDialog.showModal();
      loadApps().then(function () { renderResults(cmdkInput.value); });
      cmdkTrap.activate();
      setTimeout(function () { cmdkInput.focus(); }, 0);
    }
    function closePalette() {
      if (cmdkDialog.open) cmdkDialog.close();
    }

    cmdkDialog.addEventListener("close", function () {
      cmdkTrap.release(cmdkOpenBtn);
      cmdkInput.value = "";
    });
    cmdkDialog.addEventListener("cancel", closePalette);
    // Clicking the ::backdrop area (outside .cmdk-box) closes the dialog.
    cmdkDialog.addEventListener("click", function (e) {
      if (e.target === cmdkDialog) closePalette();
    });
    if (cmdkOpenBtn) cmdkOpenBtn.addEventListener("click", openPalette);
    cmdkInput.addEventListener("input", function () { renderResults(cmdkInput.value); });
    cmdkInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (cmdkItems.length) { cmdkSelected = (cmdkSelected + 1) % cmdkItems.length; highlightSelected(); }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (cmdkItems.length) { cmdkSelected = (cmdkSelected - 1 + cmdkItems.length) % cmdkItems.length; highlightSelected(); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (cmdkItems[cmdkSelected]) cmdkItems[cmdkSelected].click();
      }
    });
    document.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (cmdkDialog.open) closePalette(); else openPalette();
      }
    });
    window.DEL.palette = { open: openPalette, close: closePalette };
  }

  // =========================================================================
  // Stop-propagation links (domain links inside clickable table rows)
  // =========================================================================
  // Replaces an inline onclick="event.stopPropagation()", which a
  // script-src 'self' CSP blocks. Delegated so it also covers rows that
  // AG Grid or the live job poller render after load.
  document.addEventListener("click", function (e) {
    var el = e.target && e.target.closest && e.target.closest("[data-stop-propagation]");
    if (el) e.stopPropagation();
  });

  // =========================================================================
  // Collapsible sections: remember open/closed per page, expand/collapse all
  // =========================================================================
  // <details class="section" id="…" data-remember> keeps its state per path in
  // localStorage. A button with data-expand-all="open|close" (optionally
  // data-scope="#container") opens or closes every details.section in scope.
  // Opening via a #hash link to a section id also opens it.
  var SECTION_KEY = "del.sections." + location.pathname;
  var sectionState = {};
  try { sectionState = JSON.parse(localStorage.getItem(SECTION_KEY) || "{}") || {}; } catch (e) { sectionState = {}; }
  document.querySelectorAll("details.section[data-remember][id]").forEach(function (d) {
    if (Object.prototype.hasOwnProperty.call(sectionState, d.id)) d.open = !!sectionState[d.id];
    d.addEventListener("toggle", function () {
      sectionState[d.id] = d.open;
      try { localStorage.setItem(SECTION_KEY, JSON.stringify(sectionState)); } catch (e) {}
    });
  });
  function openHashTarget() {
    if (!location.hash || location.hash.length < 2) return;
    var target = null;
    try { target = document.querySelector(location.hash); } catch (e) { return; }
    var d = target && (target.matches("details") ? target : target.closest("details"));
    if (d && !d.open) d.open = true;
    if (target && target.scrollIntoView) target.scrollIntoView({ block: "start" });
  }
  openHashTarget();
  window.addEventListener("hashchange", openHashTarget);
  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest && e.target.closest("[data-expand-all]");
    if (!btn) return;
    var scopeSel = btn.getAttribute("data-scope");
    var scope = scopeSel ? document.querySelector(scopeSel) : document;
    if (!scope) return;
    var open = btn.getAttribute("data-expand-all") !== "close";
    scope.querySelectorAll("details.section").forEach(function (d) { d.open = open; });
  });
})();

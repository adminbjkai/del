/* DEL admin UI: vanilla JS, no external assets (CSP script-src 'self').
   Loaded on every authenticated page. Page-specific code lives in
   removal.js (plan builder, live gate, job polling) and gallery.js (View
   Apps); assistant.js drives the Ask dock and the assistant page. */
(function () {
  "use strict";

  var DEL = window.DEL = window.DEL || {};
  var root = document.documentElement;

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || document).querySelectorAll(sel)); }
  function escapeHtml(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  var store = {
    get: function (key, fallback) {
      try { var v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
    },
    set: function (key, value) { try { localStorage.setItem(key, value); } catch (e) {} },
    remove: function (key) { try { localStorage.removeItem(key); } catch (e) {} },
    json: function (key) { try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (e) { return null; } },
  };
  // Small inline icons for JS-built controls (same Lucide shapes as _icons.html).
  var ICONS = {
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    filter: '<path d="M4 5h16l-6 7.5V19l-4 1.5v-8L4 5Z"/>',
    columns: '<rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M9 4v16"/><path d="M15 4v16"/>',
    page: '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4"/>',
    app: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18"/>',
    bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
  };
  function icon(name) {
    return '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' + (ICONS[name] || "") + "</svg>";
  }
  function isTyping(el) {
    return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
  }
  // SQLite datetime('now') strings carry no zone but are UTC.
  function parseUtc(value) {
    if (!value) return NaN;
    var s = String(value).replace(" ", "T");
    return Date.parse(/Z$|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  }
  function formatElapsed(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
  }
  DEL.util = { escapeHtml: escapeHtml, store: store, icon: icon, parseUtc: parseUtc };

  // =========================================================================
  // Toasts (DEL.toast) — aria-live notifications
  // =========================================================================
  function showToast(message, kind, duration) {
    var region = document.getElementById("toast-region");
    if (!region) return;
    duration = duration || 3400;
    kind = kind || "info";

    var toast = document.createElement("div");
    toast.className = "toast toast-" + kind;
    toast.setAttribute("role", "status");

    var iconName = kind === "ok" ? "check" : kind === "error" ? "close" : "bolt";
    toast.innerHTML =
      '<span class="toast-ico">' + icon(iconName) + '</span>' +
      '<span class="toast-msg">' + escapeHtml(message) + '</span>' +
      '<button type="button" class="toast-close" aria-label="Dismiss">×</button>' +
      '<div class="toast-meter" aria-hidden="true"><div class="toast-meter-fill"></div></div>';

    region.appendChild(toast);

    var fill = toast.querySelector(".toast-meter-fill");
    if (fill) {
      fill.style.transition = "width " + duration + "ms linear";
      requestAnimationFrame(function () { fill.style.width = "0%"; });
    }

    function dismiss() {
      if (toast.classList.contains("is-leaving")) return;
      toast.classList.add("is-leaving");
      setTimeout(function () { toast.remove(); }, 200);
    }

    toast.querySelector(".toast-close").addEventListener("click", function (e) {
      e.stopPropagation();
      dismiss();
    });
    toast.addEventListener("click", function (e) {
      if (e.target.tagName !== "A") dismiss();
    });

    setTimeout(dismiss, duration);
  }
  DEL.toast = showToast;

  // =========================================================================
  // Theme: <html data-theme> is set before paint by theme-init.js.
  // =========================================================================
  var THEME_COLORS = { dark: "#0f1f30", light: "#edf1f4" };
  DEL.theme = {
    get: function () { return root.getAttribute("data-theme") === "light" ? "light" : "dark"; },
    set: function (theme) {
      root.setAttribute("data-theme", theme);
      $$("meta[data-theme-color]").forEach(function (meta) { meta.content = THEME_COLORS[theme]; });
      store.set("del.theme", theme);
      var btn = document.getElementById("theme-toggle");
      if (btn) {
        btn.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
        btn.setAttribute("aria-label", theme === "light" ? "Switch to dark theme" : "Switch to light theme");
      }
    },
    toggle: function () { DEL.theme.set(DEL.theme.get() === "light" ? "dark" : "light"); },
  };
  DEL.theme.set(DEL.theme.get());
  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) themeBtn.addEventListener("click", DEL.theme.toggle);

  // Widths from data-width="NN" (percent): CSP forbids style="" in markup.
  function applyWidths(scope) {
    $$("[data-width]", scope).forEach(function (el) { el.style.width = el.getAttribute("data-width") + "%"; });
  }
  applyWidths(document);

  // Storage left behind by the removed AG Grid engine and older layouts.
  try {
    for (var k = localStorage.length - 1; k >= 0; k--) {
      var key = localStorage.key(k);
      if (key && /^del\.ag\d\.colstate\.|^del\.(glossaryCollapsed|rightRailTab)$/.test(key)) localStorage.removeItem(key);
    }
  } catch (e) {}
  try { sessionStorage.removeItem("del.siteplanSeen"); } catch (e) {}

  // Apps opened this browser, newest first. The command palette lists them.
  (function rememberApp() {
    var slug = document.body.getAttribute("data-app") || "";
    if (!slug) return;
    var list = store.json("del.recentApps");
    if (!Array.isArray(list)) list = [];
    list = [slug].concat(list.filter(function (s) { return s !== slug; })).slice(0, 8);
    store.set("del.recentApps", JSON.stringify(list));
  })();

  // =========================================================================
  // Sort values: one parser for sorting, numeric filters and CSV order.
  // =========================================================================
  var SIZE_UNITS = {
    b: 1, kb: 1e3, mb: 1e6, gb: 1e9, tb: 1e12, pb: 1e15,
    kib: 1024, mib: 1048576, gib: 1073741824, tib: 1099511627776,
    k: 1e3, m: 1e6, g: 1e9, t: 1e12,
  };
  function parseSize(str) {
    var m = /^([0-9]*\.?[0-9]+)\s*([kmgtp]?i?b|[kmgt])?$/i.exec(str);
    if (!m) return null;
    var mult = SIZE_UNITS[(m[2] || "b").toLowerCase()];
    return mult === undefined ? null : parseFloat(m[1]) * mult;
  }
  // Durations ("42s", "1.5m", "2.3h") are checked before sizes, which would
  // otherwise read "1.5m" as 1.5 MB.
  var DURATION_UNITS = { s: 1, m: 60, h: 3600, d: 86400 };
  function parseDuration(str) {
    var m = /^([0-9]*\.?[0-9]+)\s*([smhd])$/.exec(str);
    return m ? parseFloat(m[1]) * DURATION_UNITS[m[2]] : null;
  }
  var ISO_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?/;
  // {n: number|null, s: string}; blank ("" or "—") is {n: null, s: ""}.
  function sortValue(raw) {
    raw = (raw == null ? "" : String(raw)).trim();
    if (raw === "" || raw === "—") return { n: null, s: "" };
    var plain = raw.replace(/,/g, "").replace(/%$/, "");
    if (/^-?[0-9]*\.?[0-9]+$/.test(plain)) return { n: parseFloat(plain), s: raw };
    // "#123" ids and ", "-joined number lists ("8083, 8767") sort by their first number.
    var lead = /^#?(\d+)(?:,\s+\d+)*$/.exec(raw);
    if (lead) return { n: parseFloat(lead[1]), s: raw };
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
  function compareValues(va, vb, dir) {
    // Blank cells stay at the bottom in both directions.
    var ea = va.n === null && va.s === "";
    var eb = vb.n === null && vb.s === "";
    if (ea || eb) return ea === eb ? 0 : (ea ? 1 : -1);
    var res;
    if (va.n !== null && vb.n !== null) res = va.n - vb.n;
    else if (va.n !== null) res = -1;
    else if (vb.n !== null) res = 1;
    else res = va.s < vb.s ? -1 : va.s > vb.s ? 1 : 0;
    return dir === "desc" ? -res : res;
  }
  DEL.sortValue = sortValue;

  // =========================================================================
  // Focus trap for modal surfaces (drawer, sheets, popovers)
  // =========================================================================
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]),' +
    ' textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  function focusableIn(container) {
    if (!container) return [];
    return $$(FOCUSABLE, container).filter(function (el) {
      return el.offsetWidth || el.offsetHeight || el.getClientRects().length;
    });
  }
  function makeFocusTrap(getContainer) {
    var lastFocused = null, active = false;
    function onKeydown(e) {
      if (e.key !== "Tab" || !active) return;
      var items = focusableIn(getContainer());
      if (!items.length) return;
      var first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    return {
      activate: function () {
        if (active) return;
        active = true;
        lastFocused = document.activeElement;
        document.addEventListener("keydown", onKeydown, true);
        setTimeout(function () { var items = focusableIn(getContainer()); if (items.length) items[0].focus(); }, 30);
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
  // Popovers anchored to a button (column filter, column chooser)
  // =========================================================================
  var openPop = null;
  function closePopover() {
    if (!openPop) return;
    var p = openPop;
    openPop = null;
    p.trap.release(p.trigger);
    p.el.remove();
    p.trigger.setAttribute("aria-expanded", "false");
  }
  function openPopover(trigger, el) {
    if (openPop && openPop.trigger === trigger) { closePopover(); return false; }
    closePopover();
    document.body.appendChild(el);
    var r = trigger.getBoundingClientRect();
    el.style.position = "absolute";
    el.style.top = (window.scrollY + r.bottom + 4) + "px";
    var maxLeft = window.scrollX + document.documentElement.clientWidth - el.offsetWidth - 8;
    el.style.left = Math.max(8, Math.min(window.scrollX + r.left, maxLeft)) + "px";
    var trap = makeFocusTrap(function () { return el; });
    trigger.setAttribute("aria-expanded", "true");
    openPop = { el: el, trigger: trigger, trap: trap };
    trap.activate();
    return true;
  }
  document.addEventListener("click", function (e) {
    if (openPop && !openPop.el.contains(e.target) && !openPop.trigger.contains(e.target)) closePopover();
  });

  // =========================================================================
  // Tables. Every table.table (not .table-plain / .job-steps) gets: quick
  // filter, sortable headers, Excel-style column filters, quick chips for
  // status-like columns, column chooser + density, resizable columns,
  // pagination, CSV export of the filtered rows, clickable rows, and a
  // per-table memory of sort / page size / hidden columns / widths.
  // =========================================================================
  var MOBILE_MQ = "(max-width: 900px)";
  function isMobile() { return !!(window.matchMedia && window.matchMedia(MOBILE_MQ).matches); }
  var QUICK_HEADERS = /^(status|kind|mode|shared|protected|state|state\/health|orphan|dangling|health|class|dirty|ssl|active|driver|proto|type|risk|removal)$/i;
  var OPS = [
    ["contains", "contains"], ["notContains", "does not contain"], ["equals", "equals"],
    ["notEqual", "does not equal"], ["startsWith", "starts with"], ["endsWith", "ends with"],
    ["gt", "greater than"], ["gte", "greater than or equal"], ["lt", "less than"],
    ["lte", "less than or equal"], ["blank", "is empty"], ["notBlank", "is not empty"],
  ];

  function cellText(cell) {
    if (!cell) return "";
    var v = cell.getAttribute("data-filter-value");
    return (v !== null ? v : cell.textContent || "").replace(/\s+/g, " ").trim();
  }
  function cellSortRaw(cell) {
    if (!cell) return "";
    var v = cell.getAttribute("data-sort-value");
    return v !== null ? v : cell.textContent || "";
  }
  function setColWidth(th, px) {
    th.style.width = px ? px + "px" : "";
    th.style.minWidth = px ? px + "px" : "";
  }
  function tableKey(table) {
    if (table.id) return "del.table." + table.id;
    var cap = table.caption ? table.caption.textContent.trim() : "";
    return "del.table." + location.pathname + "|" + cap;
  }
  function setDensity(mode) {
    root.setAttribute("data-density", mode);
    store.set("del-density", mode);
  }

  function enhanceTable(table) {
    if (table._del || table.classList.contains("job-steps") || table.classList.contains("table-plain")) return;
    var tbody = table.tBodies[0];
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!tbody || !headRow) return;
    var headers = Array.prototype.slice.call(headRow.cells);
    var allRows = Array.prototype.slice.call(tbody.rows);
    var key = tableKey(table);
    var saved = store.json(key) || {};
    var state = {
      sortCol: typeof saved.sortCol === "number" ? saved.sortCol : -1,
      sortDir: saved.sortDir === "desc" ? "desc" : "asc",
      page: 0,
      pageSize: saved.pageSize != null ? saved.pageSize : parseInt(table.getAttribute("data-page-size") || "50", 10),
      hidden: Array.isArray(saved.hidden) ? saved.hidden.filter(function (i) { return i < headers.length; }) : [],
      widths: saved.widths && typeof saved.widths === "object" ? saved.widths : {},
      query: "",
      colFilters: [],
      chips: {},
    };
    var filtered = allRows.slice();
    function persist() {
      store.set(key, JSON.stringify({
        sortCol: state.sortCol, sortDir: state.sortDir, pageSize: state.pageSize,
        hidden: state.hidden, widths: state.widths,
      }));
    }

    // Cache per-row text once: cells do not change after render.
    allRows.forEach(function (r) {
      r._delText = (r.textContent || "").replace(/\s+/g, " ").toLowerCase();
      r._delCells = Array.prototype.map.call(r.cells, function (c) { return cellText(c).toLowerCase(); });
    });

    // Numeric columns align right (counts, sizes, ports).
    headers.forEach(function (th, idx) {
      var nums = 0, total = 0;
      allRows.slice(0, 200).forEach(function (r) {
        var raw = cellSortRaw(r.cells[idx]).trim();
        if (!raw || raw === "—") return;
        total++;
        if (/^-?[0-9][0-9,]*\.?[0-9]*$/.test(raw)) nums++;
      });
      if (total && nums / total > 0.85 && !th.hasAttribute("data-nosort")) {
        th.classList.add("num");
        allRows.forEach(function (r) { if (r.cells[idx]) r.cells[idx].classList.add("num"); });
      }
    });

    // --- DOM scaffold ----------------------------------------------------
    var wrap = document.createElement("div");
    wrap.className = "table-block";
    table.parentNode.insertBefore(wrap, table);
    var toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    var chipBar = document.createElement("div");
    chipBar.className = "filter-chipbar";
    chipBar.hidden = true;
    var pills = document.createElement("div");
    pills.className = "active-filter-pills";
    pills.hidden = true;
    var scroller = document.createElement("div");
    scroller.className = "table-scroll";
    var empty = document.createElement("div");
    empty.className = "empty-state";
    empty.hidden = true;
    empty.innerHTML = '<div class="empty-state-msg">' +
      escapeHtml(table.getAttribute("data-empty") || "No rows match the current filter.") + "</div>";
    wrap.appendChild(toolbar);
    wrap.appendChild(chipBar);
    wrap.appendChild(pills);
    wrap.appendChild(scroller);
    scroller.appendChild(table);
    wrap.appendChild(empty);

    var searchWrap = document.createElement("label");
    searchWrap.className = "table-search";
    searchWrap.innerHTML = icon("search") + '<span class="sr-only">Quick filter all columns</span>';
    var search = document.createElement("input");
    search.type = "search";
    search.className = "table-filter";
    search.placeholder = table.getAttribute("data-search-placeholder") || "Filter rows…";
    search.setAttribute("aria-label", "Quick filter all columns");
    searchWrap.appendChild(search);
    toolbar.appendChild(searchWrap);

    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn btn-sm btn-ghost table-clear-filters";
    clearBtn.textContent = "Clear filters";
    clearBtn.hidden = true;
    toolbar.appendChild(clearBtn);

    var showAllBtn = null;
    if (headRow.querySelector('[data-priority="low"]')) {
      table.setAttribute("data-hide-low", "");
      showAllBtn = document.createElement("button");
      showAllBtn.type = "button";
      showAllBtn.className = "btn btn-sm table-show-all";
      showAllBtn.textContent = "Show all columns";
      showAllBtn.setAttribute("aria-pressed", "false");
      showAllBtn.addEventListener("click", function () {
        var hiding = table.hasAttribute("data-hide-low");
        table.toggleAttribute("data-hide-low", !hiding);
        showAllBtn.textContent = hiding ? "Hide secondary columns" : "Show all columns";
        showAllBtn.setAttribute("aria-pressed", hiding ? "true" : "false");
      });
      toolbar.appendChild(showAllBtn);
    }

    var status = document.createElement("div");
    status.className = "table-status";
    status.setAttribute("role", "status");
    toolbar.appendChild(status);

    var pager = document.createElement("div");
    pager.className = "table-pager";
    var prevBtn = document.createElement("button");
    prevBtn.type = "button";
    prevBtn.className = "btn btn-sm";
    prevBtn.setAttribute("aria-label", "Previous page");
    prevBtn.textContent = "‹";
    var nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "btn btn-sm";
    nextBtn.setAttribute("aria-label", "Next page");
    nextBtn.textContent = "›";
    pager.appendChild(prevBtn);
    pager.appendChild(nextBtn);
    toolbar.appendChild(pager);

    var pageSel = document.createElement("select");
    pageSel.className = "table-pagesize";
    pageSel.setAttribute("aria-label", "Rows per page");
    [25, 50, 100, 250, 0].forEach(function (n) {
      var o = document.createElement("option");
      o.value = String(n);
      o.textContent = n === 0 ? "All rows" : n + " rows";
      pageSel.appendChild(o);
    });
    if (!pageSel.querySelector('option[value="' + state.pageSize + '"]')) state.pageSize = 50;
    pageSel.value = String(state.pageSize);
    toolbar.appendChild(pageSel);

    var colBtn = document.createElement("button");
    colBtn.type = "button";
    colBtn.className = "btn btn-sm btn-ghost table-columns-btn";
    colBtn.setAttribute("aria-haspopup", "dialog");
    colBtn.setAttribute("aria-expanded", "false");
    colBtn.innerHTML = icon("columns") + "<span>Columns</span>";
    toolbar.appendChild(colBtn);

    // The page-level "Export CSV" button moves into this toolbar.
    var exportBtn = table.id ? $('[data-export-table="' + table.id + '"]') : null;
    if (exportBtn) {
      exportBtn.className = "btn btn-sm btn-ghost";
      exportBtn.innerHTML = icon("download") + "<span>CSV</span>";
      toolbar.appendChild(exportBtn);
    }

    // Short tables need no chrome at all.
    var minimal = allRows.length <= 10 && !exportBtn;
    toolbar.classList.toggle("is-minimal", minimal);

    // --- Headers: sort button, filter button, resizer ----------------------
    var filterBtns = [];
    headers.forEach(function (th, idx) {
      var label = (th.textContent || "").trim();
      var inner = document.createElement("div");
      inner.className = "th-inner";
      if (th.hasAttribute("data-nosort")) {
        inner.innerHTML = th.innerHTML;
        th.innerHTML = "";
        th.appendChild(inner);
        return;
      }
      th.setAttribute("aria-sort", "none");
      var sortBtn = document.createElement("button");
      sortBtn.type = "button";
      sortBtn.className = "th-sort-btn";
      sortBtn.innerHTML = th.innerHTML;
      sortBtn.setAttribute("aria-label", "Sort by " + label);
      sortBtn.addEventListener("click", function () {
        if (state.sortCol === idx) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        else { state.sortCol = idx; state.sortDir = "asc"; }
        persist();
        render();
      });
      th.innerHTML = "";
      inner.appendChild(sortBtn);

      var f = { col: idx, label: label, op: "contains", val: "", selected: null, exclude: false };
      state.colFilters.push(f);
      var fb = document.createElement("button");
      fb.type = "button";
      fb.className = "th-filter-btn";
      fb.setAttribute("aria-label", "Filter " + label);
      fb.setAttribute("aria-haspopup", "dialog");
      fb.setAttribute("aria-expanded", "false");
      fb.title = "Filter this column";
      fb.innerHTML = icon("filter");
      fb.addEventListener("click", function (e) { e.stopPropagation(); openFilter(f, fb); });
      filterBtns[idx] = fb;
      inner.appendChild(fb);
      th.appendChild(inner);

      var resizer = document.createElement("span");
      resizer.className = "th-resizer";
      resizer.setAttribute("aria-hidden", "true");
      resizer.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        var startX = e.clientX, startW = th.offsetWidth;
        resizer.setPointerCapture(e.pointerId);
        // Auto table layout honours min-width, not width, so a column can be
        // widened freely and narrowed down to its content's minimum.
        function move(ev) { setColWidth(th, Math.max(56, startW + ev.clientX - startX)); }
        function up() {
          resizer.removeEventListener("pointermove", move);
          resizer.removeEventListener("pointerup", up);
          state.widths[idx] = th.offsetWidth;
          persist();
          checkWide();
        }
        resizer.addEventListener("pointermove", move);
        resizer.addEventListener("pointerup", up);
      });
      resizer.addEventListener("dblclick", function () { setColWidth(th, null); delete state.widths[idx]; persist(); checkWide(); });
      th.appendChild(resizer);
      if (state.widths[idx]) setColWidth(th, state.widths[idx]);
    });

    function filterActive(f) {
      return !!f.val.trim() || f.selected !== null || f.op === "blank" || f.op === "notBlank";
    }

    function openFilter(f, btn) {
      var values = {};
      allRows.forEach(function (r) {
        var v = cellText(r.cells[f.col]);
        values[v === "—" ? "" : v] = true;
      });
      var valueList = Object.keys(values).sort();
      var pop = document.createElement("div");
      pop.className = "col-filter-pop";
      pop.setAttribute("role", "dialog");
      pop.setAttribute("aria-label", "Filter " + f.label);
      pop.innerHTML =
        '<div class="col-filter-pop-title">Filter ' + escapeHtml(f.label) + "</div>" +
        '<div class="col-filter-pop-head"><label>Show rows where the value' +
        '<select class="col-filter-op" aria-label="Filter operator">' +
        OPS.map(function (o) { return '<option value="' + o[0] + '">' + o[1] + "</option>"; }).join("") +
        '</select><input type="text" class="col-filter-text" placeholder="Value…" aria-label="Filter value"></label></div>' +
        '<label class="col-filter-exclude"><input type="checkbox" class="col-filter-exclude-cb"> Exclude the ticked values</label>' +
        '<div class="col-filter-actions"><button type="button" class="btn btn-sm col-filter-all">Select all</button>' +
        '<button type="button" class="btn btn-sm col-filter-none">Clear list</button></div>' +
        '<input type="search" class="col-filter-search" placeholder="Find a value…" aria-label="Find a value">' +
        '<div class="col-filter-list" role="group" aria-label="Values"></div>' +
        '<div class="col-filter-close-row"><button type="button" class="btn btn-sm btn-primary col-filter-done">Done</button></div>';
      var opEl = $(".col-filter-op", pop), textEl = $(".col-filter-text", pop);
      var findEl = $(".col-filter-search", pop), listEl = $(".col-filter-list", pop);
      var excludeEl = $(".col-filter-exclude-cb", pop);
      opEl.value = f.op;
      textEl.value = f.val;
      excludeEl.checked = f.exclude;
      textEl.disabled = f.op === "blank" || f.op === "notBlank";
      function changed() { btn.classList.toggle("is-active", filterActive(f)); applyFilter(); render(); }
      function renderList() {
        var q = findEl.value.toLowerCase();
        listEl.innerHTML = "";
        valueList.forEach(function (v) {
          var text = v === "" ? "(blank)" : v;
          if (q && text.toLowerCase().indexOf(q) === -1) return;
          var lab = document.createElement("label");
          lab.className = "col-filter-item";
          var cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = f.selected === null || f.selected.has(v);
          cb.addEventListener("change", function () {
            if (f.selected === null) f.selected = new Set(valueList);
            if (cb.checked) f.selected.add(v); else f.selected.delete(v);
            if (f.selected.size === valueList.length) f.selected = null;
            changed();
          });
          var span = document.createElement("span");
          span.textContent = text;
          span.title = text;
          lab.appendChild(cb);
          lab.appendChild(span);
          listEl.appendChild(lab);
        });
        if (!listEl.childNodes.length) listEl.innerHTML = '<div class="faint">No values</div>';
      }
      renderList();
      $(".col-filter-all", pop).addEventListener("click", function () { f.selected = null; renderList(); changed(); });
      $(".col-filter-none", pop).addEventListener("click", function () { f.selected = new Set(); renderList(); changed(); });
      excludeEl.addEventListener("change", function () { f.exclude = excludeEl.checked; changed(); });
      opEl.addEventListener("change", function () {
        f.op = opEl.value;
        textEl.disabled = f.op === "blank" || f.op === "notBlank";
        if (textEl.disabled) { textEl.value = ""; f.val = ""; }
        changed();
      });
      textEl.addEventListener("input", function () { f.val = textEl.value; changed(); });
      findEl.addEventListener("input", renderList);
      $(".col-filter-done", pop).addEventListener("click", closePopover);
      openPopover(btn, pop);
    }

    // --- Column chooser + density ----------------------------------------
    colBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var pop = document.createElement("div");
      pop.className = "col-menu";
      pop.setAttribute("role", "dialog");
      pop.setAttribute("aria-label", "Columns and density");
      var html = '<div class="col-menu-title">Show columns</div>';
      headers.forEach(function (th, idx) {
        var label = (th.textContent || "").trim() || "Column " + (idx + 1);
        html += '<label><input type="checkbox" data-col="' + idx + '"' +
          (state.hidden.indexOf(idx) === -1 ? " checked" : "") + "> " + escapeHtml(label) + "</label>";
      });
      var compact = root.getAttribute("data-density") === "compact";
      html += '<div class="col-menu-foot"><label><input type="checkbox" class="col-menu-density"' +
        (compact ? " checked" : "") + "> Compact rows</label>" +
        '<button type="button" class="btn btn-sm btn-ghost col-menu-reset">Reset table</button></div>';
      pop.innerHTML = html;
      $$("input[data-col]", pop).forEach(function (cb) {
        cb.addEventListener("change", function () {
          var idx = Number(cb.getAttribute("data-col"));
          state.hidden = state.hidden.filter(function (i) { return i !== idx; });
          if (!cb.checked) state.hidden.push(idx);
          persist();
          applyHidden();
        });
      });
      $(".col-menu-density", pop).addEventListener("change", function () {
        setDensity(this.checked ? "compact" : "comfortable");
      });
      $(".col-menu-reset", pop).addEventListener("click", function () {
        store.remove(key);
        state.sortCol = -1; state.sortDir = "asc"; state.hidden = []; state.widths = {};
        state.pageSize = parseInt(table.getAttribute("data-page-size") || "50", 10);
        pageSel.value = String(state.pageSize);
        headers.forEach(function (th) { setColWidth(th, null); });
        closePopover();
        applyHidden();
        render();
      });
      openPopover(colBtn, pop);
    });

    function applyHidden() {
      headers.forEach(function (th, idx) {
        var hide = state.hidden.indexOf(idx) !== -1;
        th.hidden = hide;
        allRows.forEach(function (r) { if (r.cells[idx]) r.cells[idx].hidden = hide; });
      });
      checkWide();
    }

    // --- Quick chips for status-like columns ------------------------------
    headers.forEach(function (th, idx) {
      var label = (th.textContent || "").trim();
      if (!QUICK_HEADERS.test(label)) return;
      var values = {};
      allRows.forEach(function (r) {
        cellText(r.cells[idx]).split(/\s+/).forEach(function (part) { if (part && part !== "—") values[part] = true; });
      });
      var keys = Object.keys(values).sort();
      if (keys.length < 2 || keys.length > 12) return;
      var group = document.createElement("div");
      group.className = "filter-chip-group";
      group.innerHTML = '<span class="filter-chip-label">' + escapeHtml(label) + "</span>";
      keys.forEach(function (val) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "filter-chip";
        chip.setAttribute("aria-pressed", "false");
        chip.textContent = val.replace(/_/g, " ");
        chip.addEventListener("click", function () {
          var set = state.chips[idx] || new Set();
          if (set.has(val)) set.delete(val); else set.add(val);
          if (set.size) state.chips[idx] = set; else delete state.chips[idx];
          chip.setAttribute("aria-pressed", set.has(val) ? "true" : "false");
          chip.classList.toggle("is-on", set.has(val));
          applyFilter();
          render();
        });
        group.appendChild(chip);
      });
      chipBar.appendChild(group);
      chipBar.hidden = minimal;
    });

    // --- Filtering ---------------------------------------------------------
    function matchOp(text, op, val) {
      if (op === "blank") return !text || text === "—";
      if (op === "notBlank") return !!text && text !== "—";
      if (!val) return true;
      if (op === "contains") return text.indexOf(val) !== -1;
      if (op === "notContains") return text.indexOf(val) === -1;
      if (op === "equals") return text === val;
      if (op === "notEqual") return text !== val;
      if (op === "startsWith") return text.indexOf(val) === 0;
      if (op === "endsWith") return text.slice(-val.length) === val;
      var n = sortValue(val).n;
      return n === null ? true : null; // numeric ops handled by caller
    }
    function applyFilter() {
      var term = state.query.trim().toLowerCase();
      filtered = allRows.filter(function (r) {
        if (term && r._delText.indexOf(term) === -1) return false;
        for (var chipCol in state.chips) {
          var text = r._delCells[chipCol] || "", hit = false;
          state.chips[chipCol].forEach(function (v) { if (text.indexOf(v.toLowerCase()) !== -1) hit = true; });
          if (!hit) return false;
        }
        for (var i = 0; i < state.colFilters.length; i++) {
          var f = state.colFilters[i];
          if (!filterActive(f)) continue;
          var t = r._delCells[f.col] || "";
          if (f.selected !== null) {
            var raw = t === "—" ? "" : t, inSet = false;
            f.selected.forEach(function (v) { if (v.toLowerCase() === raw) inSet = true; });
            if (f.exclude ? inSet : !inSet) return false;
          }
          var val = f.val.trim().toLowerCase();
          if (/^(gt|gte|lt|lte)$/.test(f.op)) {
            if (!val) continue;
            var a = sortValue(cellSortRaw(r.cells[f.col])).n, b = sortValue(val).n;
            if (a === null || b === null) return false;
            if ((f.op === "gt" && !(a > b)) || (f.op === "gte" && !(a >= b)) ||
                (f.op === "lt" && !(a < b)) || (f.op === "lte" && !(a <= b))) return false;
            continue;
          }
          if (!matchOp(t, f.op, val)) return false;
        }
        return true;
      });
      state.page = 0;
    }

    function updatePills() {
      pills.innerHTML = "";
      state.colFilters.forEach(function (f) {
        if (!filterActive(f)) return;
        var parts = [];
        if (f.op === "blank" || f.op === "notBlank") parts.push(f.op === "blank" ? "is empty" : "is not empty");
        else if (f.val.trim()) parts.push(OPS.filter(function (o) { return o[0] === f.op; })[0][1] + " “" + f.val.trim() + "”");
        if (f.selected !== null) parts.push((f.exclude ? "not " : "") + f.selected.size + " value" + (f.selected.size === 1 ? "" : "s"));
        var pill = document.createElement("button");
        pill.type = "button";
        pill.className = "filter-chip is-on";
        pill.textContent = f.label + ": " + parts.join(", ") + " ×";
        pill.setAttribute("aria-label", "Clear the filter on " + f.label);
        pill.addEventListener("click", function () {
          f.op = "contains"; f.val = ""; f.selected = null; f.exclude = false;
          if (filterBtns[f.col]) filterBtns[f.col].classList.remove("is-active");
          applyFilter();
          render();
        });
        pills.appendChild(pill);
      });
      pills.hidden = !pills.childNodes.length;
    }

    // --- Render ------------------------------------------------------------
    function render() {
      if (state.sortCol >= 0 && state.sortCol < headers.length) {
        var col = state.sortCol, dir = state.sortDir;
        filtered.sort(function (a, b) {
          return compareValues(sortValue(cellSortRaw(a.cells[col])), sortValue(cellSortRaw(b.cells[col])), dir);
        });
      }
      headers.forEach(function (th, idx) {
        if (!th.hasAttribute("aria-sort")) return;
        th.setAttribute("aria-sort", idx === state.sortCol ? (state.sortDir === "asc" ? "ascending" : "descending") : "none");
      });
      var size = state.pageSize, total = filtered.length, start = 0, end = total;
      if (size > 0) {
        var pages = Math.max(1, Math.ceil(total / size));
        if (state.page >= pages) state.page = pages - 1;
        start = state.page * size;
        end = Math.min(start + size, total);
      }
      var frag = document.createDocumentFragment();
      for (var i = start; i < end; i++) frag.appendChild(filtered[i]);
      tbody.textContent = "";
      tbody.appendChild(frag);
      empty.hidden = total !== 0;
      var msg = (total ? start + 1 : 0) + "–" + end + " of " + total;
      if (total !== allRows.length) msg += " (filtered from " + allRows.length + ")";
      status.textContent = msg;
      pager.hidden = !(size > 0 && total > size);
      prevBtn.disabled = state.page <= 0;
      nextBtn.disabled = end >= total;
      pageSel.hidden = allRows.length <= 25;
      var filtering = !!state.query.trim() || Object.keys(state.chips).length > 0 ||
        state.colFilters.some(filterActive);
      clearBtn.hidden = !filtering;
      updatePills();
    }

    function checkWide() {
      scroller.classList.remove("is-wide");
      if (table.scrollWidth > scroller.clientWidth + 2) scroller.classList.add("is-wide");
    }

    // --- Wiring --------------------------------------------------------------
    var searchTimer = null;
    search.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { state.query = search.value; applyFilter(); render(); }, 80);
    });
    clearBtn.addEventListener("click", function () {
      search.value = "";
      state.query = "";
      closePopover();
      state.colFilters.forEach(function (f) { f.op = "contains"; f.val = ""; f.selected = null; f.exclude = false; });
      state.chips = {};
      $$(".filter-chip.is-on", chipBar).forEach(function (c) { c.classList.remove("is-on"); c.setAttribute("aria-pressed", "false"); });
      filterBtns.forEach(function (b) { if (b) b.classList.remove("is-active"); });
      applyFilter();
      render();
    });
    pageSel.addEventListener("change", function () {
      state.pageSize = parseInt(pageSel.value, 10);
      state.page = 0;
      persist();
      render();
    });
    prevBtn.addEventListener("click", function () { if (state.page > 0) { state.page--; render(); } });
    nextBtn.addEventListener("click", function () { state.page++; render(); });

    // Clicking a row opens its first in-app link (the row's subject).
    tbody.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input, select, textarea, label, summary, details, form, .copy")) return;
      if (window.getSelection && String(window.getSelection())) return;
      var tr = e.target.closest("tr");
      var link = tr && tr.cells[0] && tr.cells[0].querySelector('a[href^="/"]');
      if (!link) return;
      if (e.ctrlKey || e.metaKey) window.open(link.href, "_blank", "noopener");
      else location.href = link.href;
    });
    allRows.forEach(function (r) {
      if (r.cells[0] && r.cells[0].querySelector('a[href^="/"]')) r.classList.add("is-link");
    });

    // CSV export reads every filtered + sorted row, not just this page.
    table._delFilteredRows = function () { return filtered; };

    var prefill = table.getAttribute("data-prefill");
    if (prefill) { search.value = prefill; state.query = prefill; }
    table._del = true;
    applyHidden();
    applyFilter();
    render();
    checkWide();
    wrap._delCheckWide = checkWide;
  }

  $$("table.table").forEach(enhanceTable);
  var wideTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(wideTimer);
    wideTimer = setTimeout(function () { $$(".table-block").forEach(function (w) { if (w._delCheckWide) w._delCheckWide(); }); }, 150);
  });
  // Tables inside closed sections/tabs measure 0 wide until shown.
  document.addEventListener("toggle", function (e) {
    if (e.target.open) $$(".table-block", e.target).forEach(function (w) { if (w._delCheckWide) w._delCheckWide(); });
  }, true);

  // --- CSV export -----------------------------------------------------------
  function csvEscape(val) {
    var s = (val == null ? "" : String(val)).replace(/\r?\n/g, " ").trim();
    return /[",]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function exportTableCsv(tableId) {
    var table = document.getElementById(tableId);
    var headRow = table && table.tHead ? table.tHead.rows[0] : null;
    if (!headRow) return;
    var lines = [Array.prototype.map.call(headRow.cells, function (th) { return csvEscape(th.textContent); }).join(",")];
    var rows = table._delFilteredRows ? table._delFilteredRows() : Array.prototype.slice.call(table.tBodies[0].rows);
    rows.forEach(function (r) {
      lines.push(Array.prototype.map.call(r.cells, function (td) {
        var v = td.getAttribute("data-sort-value");
        return csvEscape(v !== null && v !== "" ? v : td.textContent);
      }).join(","));
    });
    var url = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" }));
    var a = document.createElement("a");
    a.href = url;
    a.download = tableId + ".csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    showToast("Exported " + rows.length + " row" + (rows.length === 1 ? "" : "s"), "ok");
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-export-table]");
    if (btn) exportTableCsv(btn.getAttribute("data-export-table"));
  });

  // =========================================================================
  // Small behaviours: copy, flash, confirm, submit guard, pending label
  // =========================================================================
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest("[data-copy]");
    if (!el) return;
    var text = el.getAttribute("data-copy");
    function done() {
      el.classList.add("copied");
      showToast("Copied " + (text.length > 40 ? text.slice(0, 40) + "…" : text), "ok");
      setTimeout(function () { el.classList.remove("copied"); }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {});
    } else {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      ta.remove();
    }
  });

  // Flash messages: always dismissible; only successes time out. The
  // ?flash= / ?error= parameters are dropped from the address bar so a
  // reload does not repeat them.
  function removeFlash(f) {
    f.style.transition = "opacity .35s";
    f.style.opacity = "0";
    setTimeout(function () { f.remove(); }, 380);
  }
  $$(".flash").forEach(function (f) {
    var close = document.createElement("button");
    close.type = "button";
    close.className = "flash-dismiss";
    close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "×";
    close.addEventListener("click", function () { removeFlash(f); });
    f.appendChild(close);
    if (f.hasAttribute("data-autodismiss")) setTimeout(function () { removeFlash(f); }, 4500);
  });
  try {
    var url = new URL(location.href);
    if (url.searchParams.has("flash") || url.searchParams.has("error")) {
      url.searchParams.delete("flash");
      url.searchParams.delete("error");
      history.replaceState(history.state, "", url.pathname + url.search + url.hash);
    }
  } catch (e) {}

  // Delegated so it also covers controls created after load.
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest("[data-confirm]");
    if (el && !window.confirm(el.getAttribute("data-confirm"))) {
      evt.preventDefault();
      evt.stopImmediatePropagation();
    }
  }, true);

  // Double-submit guard. Disabling is deferred one tick so the submitter's
  // own name/value (approve / exclude / mark shared) is still posted.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form.hasAttribute || !form.hasAttribute("data-submit-guard") || evt.defaultPrevented) return;
    if (form._delSubmitting) { evt.preventDefault(); return; }
    form._delSubmitting = true;
    var btn = evt.submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    var label = form.getAttribute("data-submit-guard");
    setTimeout(function () {
      btn.disabled = true;
      btn.classList.add("is-busy");
      if (label && !btn.classList.contains("icon-btn")) btn.textContent = label;
    }, 0);
  });

  document.addEventListener("click", function (evt) {
    var el = evt.target.closest("[data-pending-label]");
    if (!el) return;
    el.setAttribute("aria-busy", "true");
    el.classList.add("is-busy");
    el.textContent = el.getAttribute("data-pending-label");
  });

  // Domain links inside clickable rows must not also open the row.
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-stop-propagation]")) e.stopPropagation();
  });

  // =========================================================================
  // Shell: desktop collapse + resize, mobile drawer
  // =========================================================================
  var layout = document.getElementById("app-layout");
  var sidebar = document.getElementById("sidebar");
  var navToggle = document.getElementById("nav-toggle");
  var sidebarCollapse = document.getElementById("sidebar-collapse");
  var navBackdrop = document.getElementById("nav-backdrop");
  var navDrawer = document.getElementById("nav-drawer");

  function makePanelResizable(handle, panel, cssVar, storageKey, min, max, initial, direction) {
    if (!handle || !panel || !layout) return;
    function apply(value, remember) {
      var width = Math.max(min, Math.min(max, Math.round(value)));
      layout.style.setProperty(cssVar, width + "px");
      handle.setAttribute("aria-valuenow", String(width));
      if (remember !== false) store.set(storageKey, String(width));
    }
    var stored = parseInt(store.get(storageKey, ""), 10);
    if (Number.isFinite(stored)) apply(stored, false);
    handle.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      event.preventDefault();
      var startX = event.clientX, startWidth = panel.getBoundingClientRect().width;
      handle.classList.add("is-dragging");
      document.body.classList.add("is-panel-resizing");
      handle.setPointerCapture(event.pointerId);
      function move(ev) { apply(startWidth + (ev.clientX - startX) * direction); }
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
      var step = (event.shiftKey ? 32 : 12) * (event.key === "ArrowRight" ? 1 : -1);
      apply(panel.getBoundingClientRect().width + step * direction);
    });
    handle.addEventListener("dblclick", function () {
      layout.style.removeProperty(cssVar);
      store.remove(storageKey);
      handle.setAttribute("aria-valuenow", String(initial));
    });
  }
  makePanelResizable(document.getElementById("sidebar-resizer"), sidebar, "--sidebar-open-width", "del.sidebarWidth", 188, 320, 220, 1);

  var navTrap = makeFocusTrap(function () { return navDrawer; });
  function setSidebarCollapsed(collapsed) {
    if (!layout) return;
    if (isMobile()) { layout.classList.remove("sidebar-collapsed"); return; }
    closeMobileNav();
    layout.classList.toggle("sidebar-collapsed", !!collapsed);
    store.set("del.sidebarCollapsed", collapsed ? "1" : "0");
    if (sidebarCollapse) {
      var label = collapsed ? "Expand sidebar" : "Collapse sidebar";
      sidebarCollapse.setAttribute("aria-label", label);
      sidebarCollapse.title = label;
    }
  }
  function glossarySheetOpen() {
    var sheet = document.getElementById("glossary-sheet");
    return !!(sheet && !sheet.hidden);
  }
  function closeMobileNav() {
    var wasOpen = !!(sidebar && sidebar.classList.contains("open"));
    if (sidebar) sidebar.classList.remove("open");
    if (navToggle) { navToggle.setAttribute("aria-expanded", "false"); navToggle.setAttribute("aria-label", "Open menu"); }
    if (navBackdrop) navBackdrop.hidden = true;
    if (!glossarySheetOpen()) document.body.style.overflow = "";
    if (wasOpen) navTrap.release(navToggle);
  }
  function openMobileNav() {
    if (!sidebar || !isMobile()) return;
    sidebar.classList.add("open");
    if (navToggle) { navToggle.setAttribute("aria-expanded", "true"); navToggle.setAttribute("aria-label", "Close menu"); }
    if (navBackdrop) navBackdrop.hidden = false;
    document.body.style.overflow = "hidden";
    navTrap.activate();
  }
  function syncShellMode() {
    if (isMobile()) { if (layout) layout.classList.remove("sidebar-collapsed"); }
    else { closeMobileNav(); setSidebarCollapsed(store.get("del.sidebarCollapsed", "0") === "1"); }
  }
  syncShellMode();
  if (sidebarCollapse) sidebarCollapse.addEventListener("click", function () {
    setSidebarCollapsed(!layout.classList.contains("sidebar-collapsed"));
  });
  if (navToggle) navToggle.addEventListener("click", function () {
    if (!isMobile()) { setSidebarCollapsed(false); return; }
    if (sidebar.classList.contains("open")) closeMobileNav(); else openMobileNav();
  });
  if (navBackdrop) navBackdrop.addEventListener("click", closeMobileNav);
  if (sidebar) $$(".nav-links a", sidebar).forEach(function (a) {
    a.addEventListener("click", function () { if (isMobile()) closeMobileNav(); });
  });
  var shellTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(shellTimer);
    shellTimer = setTimeout(function () {
      syncShellMode();
      // Wide enough for the rail: the Help sheet hands the glossary back.
      if (wideRail()) closeGlossary();
    }, 100);
  });

  // =========================================================================
  // Right rail: Help (context glossary) and Ask (assistant dock)
  // =========================================================================
  var railCollapseBtn = document.getElementById("glossary-collapse");
  var rail = document.getElementById("glossary-rail");
  makePanelResizable(document.getElementById("rail-resizer"), rail, "--rail-open-width", "del.rightRailWidth", 280, 520, 320, -1);
  function wideRail() { return !(window.matchMedia && window.matchMedia("(max-width: 1279px)").matches); }
  function setRailCollapsed(collapsed) {
    if (!layout) return;
    if (!collapsed || wideRail()) layout.classList.toggle("glossary-collapsed", !!collapsed);
    if (collapsed) layout.classList.remove("ask-open");
    if (railCollapseBtn) {
      var label = collapsed ? "Show help panel" : "Hide help panel";
      railCollapseBtn.setAttribute("aria-label", label);
      railCollapseBtn.title = label;
    }
  }
  // The rail starts closed on every page; opening it lasts for this page.
  setRailCollapsed(true);
  if (railCollapseBtn) railCollapseBtn.addEventListener("click", function () {
    var collapsed = layout.classList.contains("glossary-collapsed") && !layout.classList.contains("ask-open");
    setRailCollapsed(!collapsed);
  });
  $$("[data-rail-expand]").forEach(function (btn) { btn.addEventListener("click", function () { setRailCollapsed(false); }); });

  function setRailTab(tab) {
    tab = tab === "ask" ? "ask" : "help";
    // Prompts and targets load on the first open, not on every page.
    if (tab === "ask" && DEL.assistant && DEL.assistant.ensure) DEL.assistant.ensure();
    $$(".rail-tab").forEach(function (btn) {
      var on = btn.getAttribute("data-rail-tab") === tab;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$("[data-rail-panel]").forEach(function (panel) { panel.hidden = panel.getAttribute("data-rail-panel") !== tab; });
    if (layout) layout.classList.toggle("ask-open", tab === "ask");
    if (tab === "ask") setRailCollapsed(false);
  }
  $$(".rail-tab").forEach(function (btn) {
    btn.addEventListener("click", function () { setRailTab(btn.getAttribute("data-rail-tab")); });
  });
  var askFab = document.getElementById("assistant-fab");
  if (askFab) askFab.addEventListener("click", function () { setRailTab("ask"); });

  // Ask links keep a real /assistant href as a fallback; with a dock on the
  // page they open it in place with the scope and target pre-filled.
  document.addEventListener("click", function (evt) {
    var link = evt.target.closest("[data-ask-scope]");
    if (!link || !document.getElementById("assistant-dock")) return;
    if (!(DEL.assistant && DEL.assistant.applyAsk)) return;
    evt.preventDefault();
    var scope = link.getAttribute("data-ask-scope") || "general";
    var target = link.getAttribute("data-ask-target") || "";
    var rtype = "";
    if (scope === "resource" && target.indexOf(":") !== -1) rtype = target.split(":")[0];
    if (scope === "resource_type") rtype = target;
    var draft = target
      ? "What should I know about " + target + "? Name every owner, shared, and data_loss_risk."
      : "What should I know about this screen?";
    DEL.assistant.applyAsk(scope, target, rtype, draft);
    setRailTab("ask");
  });

  // Glossary sections shown for this page: body[data-glossary] (render.py).
  var GLOSSARY_LABELS = {
    general: "General", "view-apps": "View Apps", apps: "Applications", "app-detail": "Application detail",
    orphans: "Orphans", assistant: "Assistant", jobs: "Jobs", "job-detail": "Job detail", resources: "Resources",
  };
  (function applyGlossaryContext() {
    var ctx = document.body.getAttribute("data-glossary") || "general";
    var label = GLOSSARY_LABELS[ctx] || (ctx.indexOf("resources-") === 0 ? "Resources" : ctx);
    $$(".glossary-context-label").forEach(function (el) { el.textContent = label; });
    $$(".glossary-section[data-g]").forEach(function (sec) {
      var keys = (sec.getAttribute("data-g") || "").split(/\s+/);
      var show;
      if (ctx === "orphans") show = keys.indexOf("orphans") !== -1;
      else if (ctx === "apps" || ctx === "app-detail") show = keys.indexOf(ctx) !== -1 || keys.indexOf("apps") !== -1;
      else if (ctx.indexOf("resources-") === 0) show = keys.indexOf(ctx) !== -1 || keys.indexOf("resources") !== -1;
      else show = keys.indexOf(ctx) !== -1;
      sec.classList.toggle("is-hidden", !show);
    });
  })();

  // Mobile Help sheet
  var glossaryFab = document.getElementById("glossary-fab");
  var glossarySheet = document.getElementById("glossary-sheet");
  var glossaryTrap = makeFocusTrap(function () { return glossarySheet && $(".glossary-sheet-panel", glossarySheet); });
  // One glossary per page: it lives in the rail and visits the sheet.
  var glossaryRoot = $(".glossary-root");
  var glossaryHome = glossaryRoot && glossaryRoot.parentNode;
  function openGlossary() {
    if (!glossarySheet) return;
    var sheetBody = document.getElementById("glossary-sheet-body");
    if (glossaryRoot && sheetBody) sheetBody.appendChild(glossaryRoot);
    glossarySheet.hidden = false;
    if (glossaryFab) glossaryFab.setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
    glossaryTrap.activate();
  }
  function closeGlossary() {
    if (!glossarySheet || glossarySheet.hidden) return;
    glossarySheet.hidden = true;
    if (glossaryRoot && glossaryHome) glossaryHome.appendChild(glossaryRoot);
    if (glossaryFab) glossaryFab.setAttribute("aria-expanded", "false");
    if (!sidebar || !sidebar.classList.contains("open")) document.body.style.overflow = "";
    glossaryTrap.release(glossaryFab);
  }
  function openHelp() {
    if (wideRail()) { setRailTab("help"); setRailCollapsed(false); }
    else openGlossary();
  }
  if (glossaryFab) glossaryFab.addEventListener("click", openGlossary);
  ["glossary-sheet-close", "glossary-sheet-backdrop"].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener("click", closeGlossary);
  });

  // =========================================================================
  // Scan stamp (sidebar) + Settings scan section: live while a scan runs.
  // =========================================================================
  var scanBlock = document.getElementById("scan-block");
  if (scanBlock) {
    var scanMeta = document.getElementById("scan-block-meta");
    var scanStarted = null, scanWasRunning = false, scanFailures = 0;
    var settingsLive = document.getElementById("scan-strip-live");
    var settingsElapsed = document.getElementById("scan-strip-elapsed");
    var runButtons = $$("#scan-block-run, #run-scan-btn");

    function setRunning(running, started) {
      scanBlock.classList.toggle("is-running", running);
      scanBlock.setAttribute("data-running", running ? "true" : "false");
      runButtons.forEach(function (b) { b.disabled = running; });
      if (settingsLive) settingsLive.hidden = !running;
      if (running) {
        if (!scanStarted) scanStarted = started ? parseUtc(started) : Date.now();
        if (isNaN(scanStarted)) scanStarted = Date.now();
        var text = "Scanning… " + formatElapsed(Date.now() - scanStarted);
        if (scanMeta) scanMeta.innerHTML = '<span id="scan-block-elapsed">' + escapeHtml(text) + "</span>";
        if (settingsElapsed) settingsElapsed.textContent = text;
        var dot = $(".dot", scanBlock);
        if (dot) dot.className = "dot dot-info";
      }
    }
    function pollScan() {
      fetch("/scan/status", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          scanFailures = 0;
          if (data && data.running) {
            scanWasRunning = true;
            setRunning(true, data.started);
            setTimeout(pollScan, 2500);
          } else if (scanWasRunning) {
            // Fresh inventory: reload once so every number on the page is current.
            location.reload();
          } else {
            setRunning(false);
          }
        })
        .catch(function () { if (++scanFailures < 5) setTimeout(pollScan, 4000); });
    }
    if (scanBlock.getAttribute("data-running") === "true") {
      scanWasRunning = true;
      setRunning(true, scanBlock.getAttribute("data-started"));
      setTimeout(pollScan, 2500);
    }
    // Starting a scan polls in place instead of leaving the page.
    $$(".scan-run-form").forEach(function (form) {
      form.addEventListener("submit", function (evt) {
        evt.preventDefault();
        var body = new FormData(form);
        runButtons.forEach(function (b) { b.disabled = true; });
        fetch(form.action, {
          method: "POST", body: body, credentials: "same-origin", headers: { Accept: "application/json" },
        })
          .then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (data) {
            if (data.started) {
              scanWasRunning = true;
              scanStarted = Date.now();
              setRunning(true);
              showToast("Scan started", "ok");
            } else {
              showToast(data.error || "Could not start a scan", "error");
            }
            setTimeout(pollScan, 1200);
          })
          .catch(function () { runButtons.forEach(function (b) { b.disabled = false; }); showToast("Could not start a scan", "error"); });
      });
    });
    DEL.runScan = function () {
      var form = $(".scan-run-form");
      if (form) form.requestSubmit();
    };
  }

  // =========================================================================
  // ARIA tabs: roving tabindex, arrows, deep links by #tab-id
  // =========================================================================
  function initTabs(tabRoot) {
    var tablist = $('[role="tablist"]', tabRoot);
    if (!tablist) return;
    var tabs = $$('[role="tab"]', tablist);
    if (!tabs.length) return;
    function select(tab, focus, initial) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        var panel = document.getElementById(t.getAttribute("aria-controls"));
        if (panel) {
          panel.hidden = !on;
          if (on) $$(".table-block", panel).forEach(function (w) { if (w._delCheckWide) w._delCheckWide(); });
        }
      });
      if (focus) tab.focus();
      // Not on load: a fragment set before the load event makes the browser
      // scroll the tab strip to the top of the viewport.
      if (tab.id && !initial) history.replaceState(history.state, "", "#" + tab.id);
    }
    tabs.forEach(function (tab, idx) {
      tab.addEventListener("click", function () { select(tab, false); });
      tab.addEventListener("keydown", function (e) {
        var next = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") next = tabs[(idx + 1) % tabs.length];
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = tabs[(idx - 1 + tabs.length) % tabs.length];
        else if (e.key === "Home") next = tabs[0];
        else if (e.key === "End") next = tabs[tabs.length - 1];
        if (next) { e.preventDefault(); select(next, true); }
      });
    });
    function fromHash() {
      var id = location.hash.slice(1);
      return id ? tabs.filter(function (t) { return t.id === id; })[0] : null;
    }
    select(fromHash() || tabs[0], false, true);
    window.addEventListener("hashchange", function () { var t = fromHash(); if (t) select(t, false); });
  }
  $$(".tabs").forEach(initTabs);

  // =========================================================================
  // Collapsible sections: remembered per page; expand/collapse all; #hash opens
  // =========================================================================
  var SECTION_KEY = "del.sections." + location.pathname;
  var sectionState = store.json(SECTION_KEY) || {};
  $$("details.section[data-remember][id]").forEach(function (d) {
    if (Object.prototype.hasOwnProperty.call(sectionState, d.id)) d.open = !!sectionState[d.id];
    d.addEventListener("toggle", function () {
      sectionState[d.id] = d.open;
      store.set(SECTION_KEY, JSON.stringify(sectionState));
    });
  });
  function openHashTarget() {
    if (location.hash.length < 2) return;
    var target = null;
    try { target = document.querySelector(location.hash); } catch (e) { return; }
    var d = target && (target.matches("details") ? target : target.closest("details"));
    if (d && !d.open) d.open = true;
    if (target && !target.matches('[role="tab"]')) target.scrollIntoView({ block: "start" });
  }
  openHashTarget();
  window.addEventListener("hashchange", openHashTarget);
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-expand-all]");
    if (!btn) return;
    var open = btn.getAttribute("data-expand-all") !== "close";
    $$("details.section").forEach(function (d) { d.open = open; });
  });

  // =========================================================================
  // Dashboard site plan: filter, and the kind / status switch
  // =========================================================================
  var siteplan = document.getElementById("siteplan");
  if (siteplan) {
    var planFilter = document.getElementById("siteplan-filter");
    var planCount = document.getElementById("siteplan-count");
    var planEmpty = document.getElementById("siteplan-empty");
    var planLots = $$(".lot", siteplan);
    var groupBtns = $$("[data-siteplan-group]", siteplan);
    function activeView() {
      var views = $$("[data-siteplan-view]", siteplan);
      for (var i = 0; i < views.length; i++) if (!views[i].hidden) return views[i];
      return views[0] || null;
    }
    function applyPlanFilter() {
      var q = planFilter ? planFilter.value.trim().toLowerCase() : "";
      var activeChip = $(".filter-chip.is-on", siteplan);
      var statusFilter = activeChip ? activeChip.getAttribute("data-lot-filter") : "all";
      if (statusFilter === "all") statusFilter = "";

      planLots.forEach(function (lot) {
        var hay = (lot.getAttribute("data-find") || "").toLowerCase();
        var matchQ = !q || hay.indexOf(q) !== -1;
        var matchStatus = !statusFilter || lot.classList.contains("is-" + statusFilter) || (statusFilter === "warning" && lot.classList.contains("has-warning"));
        lot.parentElement.hidden = !(matchQ && matchStatus);
      });
      $$(".siteplan-group", siteplan).forEach(function (group) {
        var shown = 0;
        $$("li", group).forEach(function (li) { if (!li.hidden) shown += 1; });
        group.hidden = shown === 0;
        var c = $(".siteplan-group-count", group);
        if (c) {
          var n = (q || statusFilter) ? shown : Number(c.getAttribute("data-count") || shown);
          c.textContent = n + (n === 1 ? " app" : " apps");
        }
      });
      var view = activeView();
      var visible = 0;
      var total = 0;
      if (view) {
        $$("li", view).forEach(function (li) { total += 1; if (!li.hidden) visible += 1; });
      }
      if (planCount) planCount.textContent = (q || statusFilter) ? (visible + " of " + total) : "";
      if (planEmpty) planEmpty.hidden = !(q || statusFilter) || visible > 0;
    }
    $$("[data-lot-filter]", siteplan).forEach(function (btn) {
      btn.addEventListener("click", function () {
        $$("[data-lot-filter]", siteplan).forEach(function (b) { b.classList.remove("is-on"); });
        btn.classList.add("is-on");
        applyPlanFilter();
      });
    });
    function showGroup(mode) {
      groupBtns.forEach(function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-siteplan-group") === mode ? "true" : "false"); });
      $$("[data-siteplan-view]", siteplan).forEach(function (v) { v.hidden = v.getAttribute("data-siteplan-view") !== mode; });
      store.set("del.siteplanGroup", mode);
      applyPlanFilter();
    }
    groupBtns.forEach(function (b) { b.addEventListener("click", function () { showGroup(b.getAttribute("data-siteplan-group")); }); });
    if (planFilter) {
      planFilter.addEventListener("input", applyPlanFilter);
      planFilter.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && planFilter.value) {
          e.stopPropagation();
          planFilter.value = "";
          applyPlanFilter();
          return;
        }
        if (e.key !== "Enter") return;
        e.preventDefault();
        var view = activeView();
        var first = view && $("li:not([hidden]) .lot", view);
        if (first) location.href = first.getAttribute("href");
      });
    }
    if (groupBtns.length) showGroup(store.get("del.siteplanGroup", "kind") === "status" ? "status" : "kind");
  }

  // =========================================================================
  // Command palette (Ctrl/Cmd+K): pages, apps, their sites, and actions
  // =========================================================================
  var cmdk = document.getElementById("cmdk");
  var shortcuts = document.getElementById("shortcuts");
  function openDialog(dialog) {
    if (dialog && typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
  }
  $$("[data-dialog-close]").forEach(function (btn) {
    btn.addEventListener("click", function () { var d = btn.closest("dialog"); if (d) d.close(); });
  });
  if (shortcuts) shortcuts.addEventListener("click", function (e) { if (e.target === shortcuts) shortcuts.close(); });

  if (cmdk && typeof cmdk.showModal === "function") {
    var cmdkInput = document.getElementById("cmdk-input");
    var cmdkResults = document.getElementById("cmdk-results");
    var cmdkEmpty = document.getElementById("cmdk-empty");
    var cmdkOpenBtn = document.getElementById("cmdk-open");
    var pages = $$("#sidebar .nav-links a").map(function (a) {
      return { title: a.textContent.trim(), url: a.getAttribute("href"), keys: a.getAttribute("data-nav-key") };
    });
    var actions = [
      { title: "Run a new scan", run: function () { if (DEL.runScan) DEL.runScan(); }, when: function () { return !!DEL.runScan; } },
      { title: "Switch light / dark theme", hint: "t", run: function () { DEL.theme.toggle(); } },
      { title: "Toggle table density (compact / comfortable)", hint: "c", run: function () { var cur = root.getAttribute("data-density") === "compact" ? "comfortable" : "compact"; setDensity(cur); DEL.toast("Table density: " + cur, "info", 1800); } },
      { title: "Open View Apps launcher", hint: "g v", run: function () { location.href = "/view-apps"; } },
      { title: "Review orphaned resources", hint: "g o", run: function () { location.href = "/orphans"; } },
      { title: "Show keyboard shortcuts", hint: "?", run: function () { openDialog(shortcuts); } },
      { title: "Open the Help panel", hint: "h", run: openHelp },
      { title: "Ask the assistant about this page", run: function () { setRailTab("ask"); }, when: function () { return !!document.getElementById("assistant-dock"); } },
      { title: "Export this table as CSV", run: function () { var b = $("[data-export-table]"); if (b) b.click(); }, when: function () { return !!$("[data-export-table]"); } },
      { title: "Collapse or expand the sidebar", hint: "[", run: function () { if (sidebarCollapse) sidebarCollapse.click(); }, when: function () { return !isMobile(); } },
    ];
    var apps = null, selected = 0, items = [];
    function loadApps() {
      if (apps !== null) return Promise.resolve();
      return fetch("/palette.json", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { apps: [] }; })
        .then(function (data) { apps = (data && data.apps) || []; })
        .catch(function () { apps = []; });
    }
    function has(text, q) { return (text || "").toLowerCase().indexOf(q) !== -1; }
    function render(query) {
      var q = (query || "").trim().toLowerCase();
      cmdkResults.innerHTML = "";
      items = [];
      function group(label, list, build) {
        if (!list.length) return;
        var h = document.createElement("div");
        h.className = "cmdk-group-label";
        h.textContent = label;
        cmdkResults.appendChild(h);
        list.forEach(function (entry) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "cmdk-item";
          btn.setAttribute("role", "option");
          btn.innerHTML = build(entry);
          btn.addEventListener("click", function () { activate(entry); });
          cmdkResults.appendChild(btn);
          items.push(btn);
        });
      }
      group("Pages", pages.filter(function (p) { return !q || has(p.title, q) || has(p.url, q); }), function (p) {
        return icon("page") + escapeHtml(p.title) + (p.keys ? '<span class="cmdk-meta"><kbd>g</kbd><kbd>' + escapeHtml(p.keys) + "</kbd></span>" : "");
      });
      var recentSlugs = {};
      var recent = [];
      if (!q && apps) {
        var wanted = store.json("del.recentApps");
        var bySlug = {};
        apps.forEach(function (a) { bySlug[a.slug] = a; });
        if (Array.isArray(wanted)) {
          wanted.forEach(function (slug) {
            if (bySlug[slug] && recent.length < 6) recent.push(bySlug[slug]);
          });
        }
        recent.forEach(function (a) { recentSlugs[a.slug] = true; });
      }
      function appEntry(a) { return { title: a.name, url: a.url, status: a.status, ports: a.ports }; }
      function appHtml(a) {
        var portsText = (a.ports && a.ports.length) ? ('<span class="cmdk-meta-port">:' + escapeHtml(a.ports.slice(0, 2).join(", :")) + '</span>') : "";
        return icon("app") + escapeHtml(a.title) + '<span class="cmdk-meta">' + portsText + '<span class="badge status-' + escapeHtml(a.status || "unknown") + '">' + escapeHtml(a.status || "") + "</span></span>";
      }
      group("Recent", recent.map(appEntry), appHtml);
      var appList = (apps || []).filter(function (a) {
        if (!q && recentSlugs[a.slug]) return false;
        return !q || has(a.name, q) || has(a.slug, q) || has((a.domains || []).join(" "), q) || has((a.ports || []).join(" "), q);
      });
      group("Applications", appList.slice(0, q ? 40 : 8).map(appEntry), appHtml);
      if (q) {
        var sites = [];
        (apps || []).forEach(function (a) {
          (a.domains || []).forEach(function (d) { if (has(d, q) || has(a.slug, q)) sites.push({ title: d, href: "https://" + d }); });
        });
        group("Open site", sites.slice(0, 8), function (s) {
          return icon("external") + escapeHtml(s.title) + '<span class="cmdk-meta">new tab</span>';
        });
      }
      group("Actions", actions.filter(function (a) { return (!a.when || a.when()) && (!q || has(a.title, q)); }), function (a) {
        return icon("bolt") + escapeHtml(a.title) + (a.hint ? '<span class="cmdk-meta"><kbd>' + escapeHtml(a.hint) + "</kbd></span>" : "");
      });
      cmdkEmpty.hidden = items.length !== 0;
      selected = 0;
      highlight();
    }
    function highlight() {
      items.forEach(function (el, i) { el.setAttribute("aria-selected", i === selected ? "true" : "false"); });
      if (items[selected]) items[selected].scrollIntoView({ block: "nearest" });
    }
    function activate(entry) {
      cmdk.close();
      if (entry.run) entry.run();
      else if (entry.href) window.open(entry.href, "_blank", "noopener");
      else if (entry.url) location.href = entry.url;
    }
    function openPalette() {
      render("");
      cmdk.showModal();
      loadApps().then(function () { render(cmdkInput.value); });
      setTimeout(function () { cmdkInput.focus(); }, 0);
    }
    cmdk.addEventListener("close", function () {
      cmdkInput.value = "";
      if (cmdkOpenBtn && !isMobile()) cmdkOpenBtn.focus();
    });
    cmdk.addEventListener("click", function (e) { if (e.target === cmdk) cmdk.close(); });
    if (cmdkOpenBtn) cmdkOpenBtn.addEventListener("click", function () { closeMobileNav(); openPalette(); });
    cmdkInput.addEventListener("input", function () { render(cmdkInput.value); });
    cmdkInput.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (items.length) {
          selected = (selected + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
          highlight();
        }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (items[selected]) items[selected].click();
      }
    });
    DEL.palette = { open: openPalette };
  }

  // =========================================================================
  // Keyboard: Ctrl/Cmd+K palette, / filter, g+key pages, t theme, ? help
  // =========================================================================
  var gPending = 0;
  document.addEventListener("keydown", function (e) {
    // Autofill and some IME events fire keydown without a key.
    var key = (e.key || "").toLowerCase();
    if ((e.ctrlKey || e.metaKey) && !e.altKey && key === "k") {
      if (!cmdk || typeof cmdk.showModal !== "function") return;
      e.preventDefault();
      if (cmdk.open) cmdk.close(); else DEL.palette.open();
      return;
    }
    if (e.key === "Escape") {
      closePopover();
      closeGlossary();
      closeMobileNav();
      if (layout && layout.classList.contains("ask-open") && !wideRail()) setRailTab("help");
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target) || document.querySelector("dialog[open]")) return;
    if (gPending && Date.now() - gPending < 1200) {
      gPending = 0;
      var link = $('#sidebar a[data-nav-key="' + key + '"]');
      if (link) { e.preventDefault(); location.href = link.getAttribute("href"); }
      return;
    }
    if (e.key === "g") { gPending = Date.now(); return; }
    if (e.key === "/") {
      // The page's own search first; otherwise the first table search that is
      // actually on screen (not inside a closed section or hidden tab).
      var filter = $("#siteplan-filter") || $("#gallery-search") || $$(".table-filter").filter(function (el) {
        return el.getClientRects().length > 0 && !el.closest("details:not([open])");
      })[0];
      if (filter) { e.preventDefault(); filter.focus(); filter.select(); }
    } else if (e.key === "t") {
      DEL.theme.toggle();
    } else if (e.key === "[" || e.key === "b") {
      e.preventDefault();
      if (sidebarCollapse && !isMobile()) sidebarCollapse.click();
    } else if (e.key === "]" || e.key === "h") {
      e.preventDefault();
      if (railCollapseBtn && wideRail()) railCollapseBtn.click();
      else if (glossaryFab) glossaryFab.click();
    } else if (e.key === "c") {
      e.preventDefault();
      var curDensity = root.getAttribute("data-density") === "compact" ? "comfortable" : "compact";
      setDensity(curDensity);
      showToast("Table density: " + curDensity, "info", 1800);
    } else if (e.key === "?") {
      e.preventDefault();
      openDialog(shortcuts);
    }
  });
})();

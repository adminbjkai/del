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
  // Tables → AG Grid Community (filters, sort, pagination) with vanilla fallback
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

  var gridApisById = {};
  var agGridReady = null;

  function loadAgGrid(cb) {
    if (window.agGrid && window.agGrid.createGrid) {
      try {
        if (window.agGrid.ModuleRegistry && window.agGrid.AllCommunityModule) {
          window.agGrid.ModuleRegistry.registerModules([window.agGrid.AllCommunityModule]);
        }
      } catch (e) { /* already registered */ }
      cb(null);
      return;
    }
    if (agGridReady) { agGridReady.push(cb); return; }
    agGridReady = [cb];
    var s = document.createElement("script");
    s.src = "/static/ag-grid-community.min.js";
    s.async = true;
    s.onload = function () {
      try {
        if (window.agGrid && window.agGrid.ModuleRegistry && window.agGrid.AllCommunityModule) {
          window.agGrid.ModuleRegistry.registerModules([window.agGrid.AllCommunityModule]);
        }
      } catch (e) {}
      var q = agGridReady || [];
      agGridReady = null;
      q.forEach(function (fn) { fn(null); });
    };
    s.onerror = function () {
      var q = agGridReady || [];
      agGridReady = null;
      q.forEach(function (fn) { fn(new Error("ag-grid load failed")); });
    };
    document.head.appendChild(s);
  }

  function delGridTheme() {
    // Clearer contrast + slightly larger type than the shell defaults.
    // The app is dark-only (both templates hardcode data-theme="dark").
    if (!window.agGrid || !window.agGrid.themeQuartz) return undefined;
    return window.agGrid.themeQuartz.withParams({
      backgroundColor: "#12151c",
      foregroundColor: "#f2f4f8",
      cellTextColor: "#f2f4f8",
      headerBackgroundColor: "#0f1218",
      headerTextColor: "#c8ceda",
      borderColor: "#2e3545",
      rowHoverColor: "#1a2030",
      oddRowBackgroundColor: "#141820",
      selectedRowBackgroundColor: "rgba(79,140,255,0.18)",
      accentColor: "#6da0ff",
      chromeBackgroundColor: "#0f1218",
      inputBackgroundColor: "#1a2030",
      inputTextColor: "#f2f4f8",
      inputPlaceholderTextColor: "#9aa3b5",
      inputBorder: { color: "#3a4254" },
      wrapperBorder: { color: "#2e3545" },
      rowBorder: { color: "#2a3140" },
      headerColumnBorder: { color: "#2e3545" },
      headerColumnResizeHandleColor: "#6da0ff",
      headerColumnResizeHandleWidth: 2,
      headerColumnResizeHandleHeight: "60%",
      columnBorder: { color: "#252b38" },
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
      fontSize: "14px",
      headerFontSize: "12.5px",
      headerFontWeight: 600,
      wrapperBorderRadius: "6px",
      browserColorScheme: "dark",
      spacing: 6,
    });
  }

  // Excel-style filter: checkbox list of existing values + text "contains / does not contain"
  function DelExcelFilter() {}
  DelExcelFilter.prototype.init = function (params) {
    this.params = params;
    this.field = params.colDef.field;
    var provided = (params.colDef.filterParams && params.colDef.filterParams.values) || [];
    this.allValues = provided.slice();
    // selected: null means "all selected" (inactive filter)
    this.selected = null;
    this.textOp = "contains";
    this.textVal = "";

    this.eGui = document.createElement("div");
    this.eGui.className = "del-excel-filter";
    this.eGui.innerHTML =
      '<div class="del-excel-filter-head">' +
      '<label class="del-excel-text-label">Text' +
      '<select class="del-excel-text-op" aria-label="Text operator">' +
      '<option value="contains">contains</option>' +
      '<option value="notContains">does not contain</option>' +
      '<option value="equals">equals</option>' +
      '<option value="notEqual">does not equal</option>' +
      '<option value="startsWith">starts with</option>' +
      '<option value="endsWith">ends with</option>' +
      "</select>" +
      '<input type="text" class="del-excel-text-inp" placeholder="Type to match…" aria-label="Text filter value">' +
      "</label>" +
      "</div>" +
      '<div class="del-excel-filter-actions">' +
      '<button type="button" class="btn btn-sm del-excel-select-all">Select all</button>' +
      '<button type="button" class="btn btn-sm del-excel-clear">Clear</button>' +
      "</div>" +
      '<input type="search" class="del-excel-search" placeholder="Search values…" aria-label="Search value list">' +
      '<div class="del-excel-list" role="group" aria-label="Filter by value"></div>';

    this.opEl = this.eGui.querySelector(".del-excel-text-op");
    this.textEl = this.eGui.querySelector(".del-excel-text-inp");
    this.searchEl = this.eGui.querySelector(".del-excel-search");
    this.listEl = this.eGui.querySelector(".del-excel-list");

    var self = this;
    this.eGui.querySelector(".del-excel-select-all").addEventListener("click", function () {
      self.selected = null; // all
      self.renderList();
      self.params.filterChangedCallback();
    });
    this.eGui.querySelector(".del-excel-clear").addEventListener("click", function () {
      self.selected = new Set(); // none
      self.renderList();
      self.params.filterChangedCallback();
    });
    this.opEl.addEventListener("change", function () {
      self.textOp = self.opEl.value;
      self.params.filterChangedCallback();
    });
    this.textEl.addEventListener("input", function () {
      self.textVal = self.textEl.value;
      self.params.filterChangedCallback();
    });
    this.searchEl.addEventListener("input", function () {
      self.renderList();
    });

    this.renderList();
  };
  DelExcelFilter.prototype.renderList = function () {
    var self = this;
    var q = (this.searchEl.value || "").toLowerCase();
    this.listEl.innerHTML = "";
    var allOn = this.selected === null;
    this.allValues.forEach(function (val) {
      var labelText = val === "" ? "(blank)" : val;
      if (q && labelText.toLowerCase().indexOf(q) === -1) return;
      var id = "del-fv-" + Math.random().toString(36).slice(2, 9);
      var lab = document.createElement("label");
      lab.className = "del-excel-item";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.id = id;
      cb.checked = allOn || (self.selected && self.selected.has(val));
      cb.addEventListener("change", function () {
        if (self.selected === null) {
          // switch from "all" to explicit set of everything except this if unchecked
          self.selected = new Set(self.allValues);
        }
        if (cb.checked) self.selected.add(val);
        else self.selected.delete(val);
        // if everything selected again, treat as inactive "all"
        if (self.selected.size === self.allValues.length) self.selected = null;
        self.params.filterChangedCallback();
      });
      var span = document.createElement("span");
      span.textContent = labelText;
      span.title = labelText;
      lab.appendChild(cb);
      lab.appendChild(span);
      self.listEl.appendChild(lab);
    });
    if (!this.listEl.childNodes.length) {
      var empty = document.createElement("div");
      empty.className = "del-excel-empty muted";
      empty.textContent = "No values";
      this.listEl.appendChild(empty);
    }
  };
  DelExcelFilter.prototype.getGui = function () { return this.eGui; };
  DelExcelFilter.prototype.doesFilterPass = function (params) {
    var raw = params.data ? params.data[this.field] : "";
    if (raw == null) raw = "";
    raw = String(raw);
    // Value set (Excel-style)
    if (this.selected !== null) {
      if (!this.selected.has(raw)) return false;
    }
    // Optional text operator
    var needle = (this.textVal || "").toLowerCase();
    if (!needle) return true;
    var hay = raw.toLowerCase();
    var op = this.textOp || "contains";
    if (op === "contains") return hay.indexOf(needle) !== -1;
    if (op === "notContains") return hay.indexOf(needle) === -1;
    if (op === "equals") return hay === needle;
    if (op === "notEqual") return hay !== needle;
    if (op === "startsWith") return hay.indexOf(needle) === 0;
    if (op === "endsWith") return hay.length >= needle.length && hay.slice(-needle.length) === needle;
    return true;
  };
  DelExcelFilter.prototype.isFilterActive = function () {
    var textOn = !!(this.textVal && String(this.textVal).trim());
    var setOn = this.selected !== null;
    return textOn || setOn;
  };
  DelExcelFilter.prototype.getModel = function () {
    if (!this.isFilterActive()) return null;
    return {
      textOp: this.textOp,
      textVal: this.textVal,
      values: this.selected === null ? null : Array.from(this.selected),
    };
  };
  DelExcelFilter.prototype.setModel = function (model) {
    if (!model) {
      this.selected = null;
      this.textOp = "contains";
      this.textVal = "";
    } else {
      this.textOp = model.textOp || "contains";
      this.textVal = model.textVal || "";
      this.selected = model.values == null ? null : new Set(model.values);
    }
    if (this.opEl) this.opEl.value = this.textOp;
    if (this.textEl) this.textEl.value = this.textVal;
    if (this.searchEl) this.searchEl.value = "";
    if (this.listEl) this.renderList();
  };
  DelExcelFilter.prototype.getModelAsString = function (model) {
    if (!model) return "";
    var parts = [];
    if (model.textVal) parts.push((model.textOp || "contains") + ' "' + model.textVal + '"');
    if (model.values) parts.push(model.values.length + " values");
    return parts.join(" · ") || "filtered";
  };

  function DelExcelFloatingFilter() {}
  DelExcelFloatingFilter.prototype.init = function (params) {
    this.params = params;
    this.eGui = document.createElement("div");
    this.eGui.className = "del-excel-ff";
    this.btn = document.createElement("button");
    this.btn.type = "button";
    this.btn.className = "del-excel-ff-btn";
    this.btn.textContent = "All ▾";
    this.btn.title = "Filter by values (Excel-style)";
    var self = this;
    this.btn.addEventListener("click", function () {
      // Open the full filter popup (value list + text ops)
      if (params.showParentFilter) params.showParentFilter();
    });
    this.eGui.appendChild(this.btn);
  };
  DelExcelFloatingFilter.prototype.onParentModelChanged = function (model) {
    if (!model) {
      this.btn.textContent = "All ▾";
      this.btn.classList.remove("is-active");
      return;
    }
    var bits = [];
    if (model.textVal) bits.push('"' + model.textVal + '"');
    if (model.values) bits.push(model.values.length + " selected");
    this.btn.textContent = (bits.join(" · ") || "Filtered") + " ▾";
    this.btn.classList.add("is-active");
  };
  DelExcelFloatingFilter.prototype.getGui = function () { return this.eGui; };

  function uniqueColumnValues(rows, field) {
    var seen = {};
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var v = rows[i][field];
      if (v == null) v = "";
      v = String(v);
      if (!Object.prototype.hasOwnProperty.call(seen, v)) {
        seen[v] = true;
        out.push(v);
      }
    }
    out.sort(function (a, b) {
      if (a === "") return -1;
      if (b === "") return 1;
      var na = parseFloat(a), nb = parseFloat(b);
      if (!isNaN(na) && !isNaN(nb) && String(a).trim() !== "" && String(b).trim() !== "") return na - nb;
      return a.toLowerCase().localeCompare(b.toLowerCase());
    });
    // Cap very high-cardinality columns for UI sanity
    if (out.length > 500) out = out.slice(0, 500);
    return out;
  }

  function parseHtmlTable(table) {
    var tbody = table.tBodies[0];
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!tbody || !headRow) return null;
    var rows = [];
    Array.prototype.forEach.call(tbody.rows, function (r) {
      var row = {};
      Array.prototype.forEach.call(r.cells, function (td, idx) {
        var text = (td.getAttribute("data-filter-value") || td.textContent || "").replace(/\s+/g, " ").trim();
        var sortV = td.getAttribute("data-sort-value");
        row["c" + idx] = text === "—" ? "" : text;
        // Prefer data-sort-value for chronological/numeric sort (e.g. Installed ISO key)
        row["s" + idx] = sortV !== null && sortV !== "" ? sortV : (text === "—" ? "" : text);
        row["h" + idx] = td.innerHTML;
      });
      row._all = (r.textContent || "").replace(/\s+/g, " ").trim();
      rows.push(row);
    });

    var colDefs = [];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      var header = (th.textContent || "").trim() || ("Col " + (idx + 1));
      var nosort = th.hasAttribute("data-nosort");
      var field = "c" + idx;
      var values = uniqueColumnValues(rows, field);
      // Detect ISO-ish sort keys (Installed / Started / Finished) for true chronological sort
      var sampleSort = "";
      for (var ri = 0; ri < rows.length; ri++) {
        if (rows[ri]["s" + idx]) { sampleSort = String(rows[ri]["s" + idx]); break; }
      }
      var isDateSort = /^\d{4}-\d{2}-\d{2}/.test(sampleSort);

      colDefs.push({
        field: field,
        headerName: header,
        sortable: !nosort,
        sortingOrder: ["asc", "desc"],
        unSortIcon: true,
        resizable: true,
        filter: DelExcelFilter,
        floatingFilter: true,
        floatingFilterComponent: DelExcelFloatingFilter,
        filterParams: {
          values: values,
        },
        minWidth: isDateSort ? 120 : 100,
        width: isDateSort ? 150 : 140,
        // autoHeight + custom HTML cells is flaky on Android Chrome (0-height rows).
        wrapText: !isDateSort,
        autoHeight: false,
        cellClass: "del-grid-cell" + (isDateSort ? " del-grid-cell-date" : ""),
        cellRenderer: function (params) {
          var html = params.data ? params.data["h" + idx] : "";
          var span = document.createElement("div");
          span.className = "del-grid-cell-inner" + (isDateSort ? " del-grid-cell-date-inner" : "");
          span.innerHTML = html || "";
          return span;
        },
        // Always sort by data-sort-value when present (ISO dates, numbers, …).
        // Uses the same sortValue() as the vanilla phone path so a column can
        // never sort one way on desktop and another on a phone.
        comparator: function (valueA, valueB, nodeA, nodeB) {
          var sa = nodeA && nodeA.data ? nodeA.data["s" + idx] : valueA;
          var sb = nodeB && nodeB.data ? nodeB.data["s" + idx] : valueB;
          sa = sa == null ? "" : String(sa);
          sb = sb == null ? "" : String(sb);
          // Blanks last in both directions feels right for Installed
          if (!sa && !sb) return 0;
          if (!sa) return 1;
          if (!sb) return -1;
          var va = sortValue(sa);
          var vb = sortValue(sb);
          if (va.n !== null && vb.n !== null) return va.n - vb.n;
          if (va.n !== null) return -1;
          if (vb.n !== null) return 1;
          return va.s < vb.s ? -1 : va.s > vb.s ? 1 : 0;
        },
        getQuickFilterText: function (params) {
          return params.data ? params.data["c" + idx] : "";
        },
      });
    });
    return { colDefs: colDefs, rows: rows };
  }

  function enhanceTableWithAgGrid(table, host) {
    var parsed = parseHtmlTable(table);
    if (!parsed) return;

    var wrap = document.createElement("div");
    wrap.className = "table-block del-grid-block";
    // `host` is the placeholder inserted before the bundle downloaded; reuse its
    // slot so the grid lands exactly where the "Loading table…" box was.
    if (host && host.parentNode) host.parentNode.insertBefore(wrap, host);
    else table.parentNode.insertBefore(wrap, table);

    var toolbar = document.createElement("div");
    toolbar.className = "table-toolbar";
    wrap.appendChild(toolbar);

    var search = document.createElement("input");
    search.type = "text";
    search.className = "table-filter";
    search.placeholder = table.getAttribute("data-search-placeholder") || "Quick filter (all columns)…";
    search.setAttribute("aria-label", "Quick filter all columns");
    toolbar.appendChild(search);

    var clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "btn btn-sm table-clear-filters";
    clearBtn.textContent = "Clear all filters";
    clearBtn.title = "Reset column filters and quick filter";
    toolbar.appendChild(clearBtn);

    var autoSizeBtn = document.createElement("button");
    autoSizeBtn.type = "button";
    autoSizeBtn.className = "btn btn-sm";
    autoSizeBtn.textContent = "Autosize columns";
    autoSizeBtn.title = "Fit each column to its content (you can still drag edges after)";
    toolbar.appendChild(autoSizeBtn);

    var fitBtn = document.createElement("button");
    fitBtn.type = "button";
    fitBtn.className = "btn btn-sm";
    fitBtn.textContent = "Fit to width";
    fitBtn.title = "Squeeze all columns into the visible width";
    toolbar.appendChild(fitBtn);

    var hint = document.createElement("span");
    hint.className = "table-resize-hint muted";
    hint.textContent = "Drag column edges to resize";
    toolbar.appendChild(hint);

    var status = document.createElement("div");
    status.className = "table-status";
    status.setAttribute("role", "status");
    wrap.appendChild(status);

    var gridHost = document.createElement("div");
    gridHost.className = "del-ag-grid";
    var defaultPage = parseInt(table.getAttribute("data-page-size") || "50", 10);
    if (!defaultPage || defaultPage < 1) defaultPage = 50;
    wrap.appendChild(gridHost);
    if (host && host.parentNode) host.parentNode.removeChild(host);

    // Short tables (dashboard panels) size to their content instead of burning
    // 72vh on three rows; long ones get an explicit height so the grid scrolls.
    var rowCount = parsed.rows.length;
    var autoHeight = rowCount <= 12;
    if (!autoHeight) {
      gridHost.style.height = Math.min(760, 96 + rowCount * 48) + "px";
    }

    // Hide original table (keep in DOM for progressive enhancement / export fallback id)
    table.style.display = "none";
    table.setAttribute("aria-hidden", "true");

    var prefill = table.getAttribute("data-prefill");
    if (prefill) search.value = prefill;

    var emptyText = table.getAttribute("data-empty") || "No rows match the current filter.";

    var api = window.agGrid.createGrid(gridHost, {
      theme: delGridTheme(),
      columnDefs: parsed.colDefs,
      rowData: parsed.rows,
      defaultColDef: {
        sortable: true,
        resizable: true,
        floatingFilter: true,
        wrapHeaderText: true,
        autoHeaderHeight: true,
        minWidth: 90,
        autoHeight: false,
      },
      rowHeight: 48,
      animateRows: false,
      suppressCellFocus: true,
      // Sideways scroll is fine when columns are wider for readability.
      suppressHorizontalScroll: false,
      alwaysShowVerticalScroll: false,
      // Live column resize by dragging header edges (default AG Grid).
      // Do NOT call sizeColumnsToFit on every resize — that undoes user widths.
      pagination: true,
      paginationPageSize: defaultPage,
      paginationPageSizeSelector: [25, 50, 100, 200],
      quickFilterText: prefill || "",
      domLayout: autoHeight ? "autoHeight" : "normal",
      // data-empty is honoured by the vanilla path too; keep the wording identical.
      overlayNoRowsTemplate:
        '<span class="table-empty">' + escapeHtml(emptyText) + "</span>",
      onGridReady: function (e) {
        try {
          // Fit first so phones/narrow panes show values immediately; desktop
          // can still Autosize from the toolbar for content-based widths.
          if (matchesMobile()) {
            e.api.sizeColumnsToFit();
          } else {
            e.api.autoSizeAllColumns(false);
          }
        } catch (err) {
          try { e.api.sizeColumnsToFit(); } catch (err2) {}
        }
      },
      onFirstDataRendered: function (e) {
        try {
          if (matchesMobile()) e.api.sizeColumnsToFit();
        } catch (err) {}
      },
      onFilterChanged: function () { updateStatus(); },
      onModelUpdated: function () { updateStatus(); },
      onPaginationChanged: function () { updateStatus(); },
      onColumnResized: function () { /* user-driven; leave widths alone */ },
    });

    function updateStatus() {
      if (!api) return;
      var total = 0;
      var displayed = 0;
      try {
        total = parsed.rows.length;
        displayed = api.getDisplayedRowCount();
      } catch (e) { return; }
      var msg = displayed + " of " + total;
      if (displayed !== total) msg += " (filtered)";
      var model = null;
      try { model = api.getFilterModel(); } catch (e) {}
      var nFilters = model ? Object.keys(model).length : 0;
      if (search.value.trim()) nFilters += 1;
      if (nFilters) msg += " · " + nFilters + " filter" + (nFilters === 1 ? "" : "s") + " active";
      status.textContent = msg;
      clearBtn.disabled = nFilters === 0 && !search.value.trim();
    }

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
    autoSizeBtn.addEventListener("click", function () {
      try { api.autoSizeAllColumns(false); } catch (e) {}
    });
    fitBtn.addEventListener("click", function () {
      try { api.sizeColumnsToFit(); } catch (e) {}
    });

    var tableId = table.id;
    if (tableId) gridApisById[tableId] = api;
    wrap._gridApi = api;
    wrap._gridSearch = search;
    updateStatus();
  }

  // Vanilla fallback: per-column contains / not-contains + clear all (no AG Grid)
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

    var scroller = document.createElement("div");
    scroller.className = "table-scroll";
    wrap.appendChild(scroller);
    scroller.appendChild(table);

    var status = document.createElement("div");
    status.className = "table-status";
    status.setAttribute("role", "status");
    wrap.appendChild(status);

    var emptyMsg = document.createElement("div");
    emptyMsg.className = "table-empty";
    emptyMsg.hidden = true;
    emptyMsg.textContent = table.getAttribute("data-empty") || "No rows match the current filter.";
    wrap.appendChild(emptyMsg);

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

    // Floating filter row: text op + Excel-style multi-select of existing values
    var filterRow = document.createElement("tr");
    filterRow.className = "table-filter-row";
    var colFilters = [];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      var td = document.createElement("th");
      td.className = "table-col-filter-cell";
      var op = document.createElement("select");
      op.className = "table-col-op";
      op.setAttribute("aria-label", "Filter operator for " + (th.textContent || "").trim());
      [
        ["contains", "contains"],
        ["notContains", "does not contain"],
        ["equals", "equals"],
        ["notEqual", "does not equal"],
        ["startsWith", "starts with"],
        ["endsWith", "ends with"],
        ["blank", "is empty"],
        ["notBlank", "is not empty"],
      ].forEach(function (pair) {
        var o = document.createElement("option");
        o.value = pair[0];
        o.textContent = pair[1];
        op.appendChild(o);
      });
      var inp = document.createElement("input");
      inp.type = "text";
      inp.className = "table-col-input";
      inp.placeholder = "text…";
      inp.setAttribute("aria-label", "Filter text for " + (th.textContent || "").trim());
      var multi = document.createElement("select");
      multi.className = "table-col-multi";
      multi.multiple = true;
      multi.size = 1;
      multi.title = "Select existing values (Ctrl/Cmd-click multi)";
      multi.setAttribute("aria-label", "Values for " + (th.textContent || "").trim());
      var values = {};
      allRows.forEach(function (r) {
        var cell = r.cells[idx];
        if (!cell) return;
        var v = (cell.getAttribute("data-filter-value") || cell.textContent || "").replace(/\s+/g, " ").trim();
        if (v === "—") v = "";
        values[v] = true;
      });
      var optAll = document.createElement("option");
      optAll.value = "";
      optAll.textContent = "(all values)";
      multi.appendChild(optAll);
      Object.keys(values).sort().forEach(function (v) {
        var o = document.createElement("option");
        o.value = v;
        o.textContent = v === "" ? "(blank)" : v;
        multi.appendChild(o);
      });
      multi.addEventListener("focus", function () { multi.size = Math.min(8, multi.options.length); });
      multi.addEventListener("blur", function () { multi.size = 1; });
      td.appendChild(op);
      td.appendChild(inp);
      td.appendChild(multi);
      filterRow.appendChild(td);
      colFilters.push({ op: op, inp: inp, multi: multi, col: idx });
      function refreshInp() {
        var need = op.value !== "blank" && op.value !== "notBlank";
        inp.disabled = !need;
        if (!need) inp.value = "";
      }
      op.addEventListener("change", function () { refreshInp(); applyFilter(); render(); });
      inp.addEventListener("input", function () { applyFilter(); render(); });
      multi.addEventListener("change", function () { applyFilter(); render(); });
      refreshInp();
    });
    if (table.tHead) table.tHead.appendChild(filterRow);

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

    function applyFilter() {
      var term = search.value.trim().toLowerCase();
      filtered = allRows.filter(function (r) {
        if (term && r.textContent.toLowerCase().indexOf(term) === -1) return false;
        for (var i = 0; i < colFilters.length; i++) {
          var f = colFilters[i];
          var op = f.op.value;
          var val = f.inp.value;
          var text = cellText(r, f.col);
          // Multi-select of existing values (Excel-like)
          var selected = [];
          Array.prototype.forEach.call(f.multi.selectedOptions, function (o) {
            if (o.value !== "" || (o.value === "" && o.textContent === "(blank)")) {
              if (o.textContent !== "(all values)") selected.push(o.value.toLowerCase());
            }
          });
          // If user selected real options (not only placeholder)
          var multiActive = false;
          Array.prototype.forEach.call(f.multi.selectedOptions, function (o) {
            if (o.textContent !== "(all values)") multiActive = true;
          });
          if (multiActive) {
            var hit = false;
            for (var s = 0; s < selected.length; s++) {
              if (text === selected[s]) { hit = true; break; }
            }
            // blank option
            if (!hit && selected.indexOf("") !== -1 && (!text || text === "—")) hit = true;
            if (!hit) return false;
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
      var hasPages = size > 0 && total > size;
      pager.hidden = !hasPages;
      prevBtn.disabled = page <= 0;
      nextBtn.disabled = size > 0 && end >= total;
    }

    search.addEventListener("input", function () { applyFilter(); render(); });
    clearBtn.addEventListener("click", function () {
      search.value = "";
      colFilters.forEach(function (f) {
        f.op.value = "contains";
        f.inp.value = "";
        f.inp.disabled = false;
        Array.prototype.forEach.call(f.multi.options, function (o) {
          o.selected = o.textContent === "(all values)";
        });
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

  function enhanceTable(table) {
    // Phones / small tablets: skip the 2MB AG Grid path — vanilla HTML tables
    // with sideways scroll reliably show cell values on Pixel / Android Chrome.
    // Same 900px breakpoint the mobile CSS uses, so the two never disagree.
    if (matchesMobile()) {
      enhanceTableVanilla(table);
      return;
    }

    // Claim the grid's slot before the 1.9 MB bundle downloads so the raw table
    // isn't shown and then yanked out from under the reader.
    var placeholder = document.createElement("div");
    placeholder.className = "del-grid-loading";
    placeholder.textContent = "Loading table…";
    table.parentNode.insertBefore(placeholder, table);
    table.style.display = "none";
    table.setAttribute("aria-hidden", "true");

    function revertToTable() {
      if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
      table.style.display = "";
      table.removeAttribute("aria-hidden");
    }

    loadAgGrid(function (err) {
      if (err || !window.agGrid || !window.agGrid.createGrid) {
        revertToTable();
        enhanceTableVanilla(table);
        return;
      }
      try {
        enhanceTableWithAgGrid(table, placeholder);
      } catch (e) {
        console.warn("AG Grid enhance failed, falling back", e);
        revertToTable();
        var orphan = table.previousElementSibling;
        if (orphan && orphan.classList && orphan.classList.contains("del-grid-block")) {
          orphan.parentNode.removeChild(orphan);
        }
        enhanceTableVanilla(table);
      }
    });
  }

  document.querySelectorAll("table[data-enhanced]").forEach(enhanceTable);

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
  // CSV export (AG Grid filtered rows, or DOM fallback)
  // =========================================================================
  function csvEscape(val) {
    var s = (val == null ? "" : String(val)).replace(/\r?\n/g, " ").trim();
    if (/[",]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function exportTableCsv(tableId) {
    var api = gridApisById[tableId];
    if (api) {
      api.exportDataAsCsv({
        fileName: (tableId || "export") + ".csv",
        exportedRows: "filteredAndSorted",
        processCellCallback: function (params) {
          // Prefer plain-text field over HTML
          if (params.column) {
            var f = params.column.getColId();
            if (params.node && params.node.data && params.node.data[f] != null) {
              return params.node.data[f];
            }
          }
          return params.value;
        },
      });
      return;
    }
    var table = document.getElementById(tableId);
    if (!table) return;
    var headRow = table.tHead ? table.tHead.rows[0] : null;
    if (!headRow) return;
    var headers = [];
    Array.prototype.forEach.call(headRow.cells, function (th) {
      if (th.classList.contains("table-col-filter-cell")) return;
      headers.push(csvEscape(th.textContent));
    });
    var lines = [headers.join(",")];
    // Prefer the vanilla renderer's filtered set: pagination removes off-page
    // rows from the DOM, so tbody.rows alone would export one page only.
    var rows = table._delFilteredRows
      ? table._delFilteredRows()
      : (table.tBodies[0] ? Array.prototype.slice.call(table.tBodies[0].rows) : []);
    rows.forEach(function (r) {
      if (r.hidden || r.style.display === "none") return;
      if (r.classList.contains("table-filter-row")) return;
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
  // View Apps: browser-local homelab gallery preferences and layout editor
  // =========================================================================
  var appGallery = document.getElementById("app-gallery");
  if (appGallery) {
    var galleryCards = Array.prototype.slice.call(appGallery.querySelectorAll(".app-launch-card"));
    var gallerySearch = document.getElementById("gallery-search");
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
          if (text.indexOf(query) === -1) return false;
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
  var navBackdrop = document.getElementById("nav-backdrop");
  var navDrawer = document.getElementById("nav-drawer");
  var SIDEBAR_KEY = "del.sidebarCollapsed";

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
      sidebarCollapse.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
      sidebarCollapse.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
      sidebarCollapse.textContent = collapsed ? "›" : "‹";
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
  var GLOSSARY_LABELS = {
    general: "General",
    "view-apps": "View Apps",
    apps: "Applications",
    "app-detail": "Application detail",
    orphans: "Orphans",
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

  document.querySelectorAll(".tabbar a[href*='/resources/']").forEach(function (a) {
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
    var TERMINAL_STATES = ["done", "failed", "success", "error", "refused"];
    var fallbackTable = document.getElementById("job-steps");

    // Build the stage panel a late-arriving step belongs to, mirroring the
    // server-rendered markup in job_detail.html.
    function stageTableFor(stage) {
      var table = outputBox.querySelector('table[data-stage="' + stage.replace(/"/g, "") + '"]');
      if (table) return table;
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
      var row = document.createElement("tr");
      row.setAttribute("data-step-seq", step.seq);
      // A job with no steps at page load renders the compact 4-column table.
      if (fallbackTable && document.contains(fallbackTable)) {
        var placeholder = fallbackTable.querySelector("tbody tr td[colspan]");
        if (placeholder) {
          var emptyRow = placeholder.parentNode;
          emptyRow.parentNode.removeChild(emptyRow);
        }
        row.innerHTML =
          "<td>" + escapeHtml(step.seq) + "</td>" +
          "<td>" + escapeHtml(step.stage || "") + "</td>" +
          '<td class="mono">' + escapeHtml(step.operation || "") + "</td>" +
          '<td class="step-state"></td>';
        fallbackTable.tBodies[0].appendChild(row);
        return row;
      }
      var table = stageTableFor(step.stage || "steps");
      row.innerHTML =
        "<td>" + escapeHtml(step.seq) + "</td>" +
        '<td class="mono">' + escapeHtml(step.operation || "") + "</td>" +
        '<td class="step-state"></td>' +
        "<td>" + (step.exit_code === null || step.exit_code === undefined ? "—" : escapeHtml(step.exit_code)) + "</td>" +
        "<td>—</td><td>—</td>";
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
        statusEl.className = "badge status-" + data.status;
      }
      (data.steps || []).forEach(function (step) {
        var row = outputBox.querySelector('tr[data-step-seq="' + step.seq + '"]');
        if (!row) {
          // Steps created after page load were polled but never rendered.
          row = appendStepRow(step);
          if (!row) return;
        }
        var stateCell = row.querySelector(".step-state");
        if (stateCell) {
          stateCell.innerHTML =
            '<span class="badge status-' + escapeHtml(step.state) + '">' + escapeHtml(step.state) + "</span>";
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
})();

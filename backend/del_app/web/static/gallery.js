/* View Apps launchpad: search, categories, favorites, grid/list, card width
   and spacing, hidden cards, drag order. Preferences stay in this browser
   (localStorage "del.appGallery.v1"); nothing here changes the inventory. */
(function () {
  "use strict";
  var gallery = document.getElementById("app-gallery");
  if (!gallery) return;
  function $(id) { return document.getElementById(id); }
  var cards = Array.prototype.slice.call(gallery.querySelectorAll(".app-launch-card"));
  var search = $("gallery-search");
  var searchOp = $("gallery-search-op");
  var category = $("gallery-category");
  var sortSel = $("gallery-sort");
  var widthIn = $("gallery-card-width");
  var widthOut = $("gallery-card-width-value");
  var densitySel = $("gallery-density");
  var layoutToggle = $("gallery-layout-toggle");
  var layoutPanel = $("gallery-layout-panel");
  var showHidden = $("gallery-show-hidden");
  var resetBtn = $("gallery-reset");
  var emptyMsg = $("gallery-empty");
  var countEl = $("gallery-visible-count");
  var KEY = "del.appGallery.v1";
  // Section order comes from the server (gallery.py _CATEGORY_ORDER).
  var ORDER = ["Favorites"].concat((gallery.getAttribute("data-category-order") || "").split("|"));
  var DEFAULTS = { view: "grid", density: "comfortable", width: 250, sort: "category", favorites: {}, hidden: {}, categories: {}, order: [] };
  function fresh() { return JSON.parse(JSON.stringify(DEFAULTS)); }
  var prefs = fresh();
  try {
    var stored = JSON.parse(localStorage.getItem(KEY) || "null");
    if (stored && typeof stored === "object") Object.keys(DEFAULTS).forEach(function (k) { if (stored[k] !== undefined) prefs[k] = stored[k]; });
  } catch (e) {}
  var editing = false, dragged = null, collapsed = {};

  function save() { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch (e) {} }
  function commit() { save(); render(); }
  function idOf(card) { return card.getAttribute("data-app-id") || ""; }
  function catOf(card) { return prefs.categories[idOf(card)] || card.getAttribute("data-category") || "Other"; }
  function compare(a, b) {
    if (prefs.sort === "custom") {
      var ia = prefs.order.indexOf(idOf(a)), ib = prefs.order.indexOf(idOf(b));
      return (ia === -1 ? 1e6 : ia) - (ib === -1 ? 1e6 : ib);
    }
    if (prefs.sort === "domain") return a.getAttribute("data-domain").localeCompare(b.getAttribute("data-domain"));
    if (prefs.sort === "latency") return Number(a.getAttribute("data-latency")) - Number(b.getAttribute("data-latency"));
    return a.getAttribute("data-name").localeCompare(b.getAttribute("data-name"));
  }
  function section(name, list, index) {
    var sec = document.createElement("section");
    sec.className = "gallery-section";
    sec.setAttribute("data-gallery-section", name);
    var gridId = "gallery-grid-" + index;
    var isCollapsed = !!collapsed[name];
    sec.innerHTML = '<div class="gallery-section-heading"><h2><button type="button" class="gallery-section-toggle" aria-expanded="' +
      (isCollapsed ? "false" : "true") + '" aria-controls="' + gridId + '"><span class="gallery-section-chevron" aria-hidden="true"></span></button></h2>' +
      '<span class="count-pill"></span></div>';
    sec.querySelector(".gallery-section-toggle").appendChild(document.createTextNode(name));
    sec.querySelector(".count-pill").textContent = String(list.length);
    var grid = document.createElement("div");
    grid.className = "gallery-grid";
    grid.id = gridId;
    grid.hidden = isCollapsed;
    list.forEach(function (card) { grid.appendChild(card); });
    sec.appendChild(grid);
    return sec;
  }
  function applyState(card) {
    var id = idOf(card);
    var fav = !!prefs.favorites[id];
    var hidden = !!prefs.hidden[id];
    card.classList.toggle("is-favorite", fav);
    card.classList.toggle("is-user-hidden", hidden);
    card.draggable = editing;
    var star = card.querySelector(".app-favorite");
    if (star) {
      var name = (card.querySelector(".app-launch-name") || {}).textContent || "app";
      star.setAttribute("aria-pressed", fav ? "true" : "false");
      star.setAttribute("aria-label", (fav ? "Remove " : "Add ") + name.trim() + (fav ? " from favorites" : " to favorites"));
    }
    var hiddenCb = card.querySelector(".gallery-card-hidden");
    if (hiddenCb) hiddenCb.checked = hidden;
    var catSel = card.querySelector(".gallery-card-category");
    if (catSel) catSel.value = catOf(card);
    var editor = card.querySelector(".app-card-editor");
    if (editor) editor.hidden = !editing;
  }
  function render() {
    var q = (search && search.value || "").trim().toLowerCase();
    var cat = category ? category.value : "";
    var withHidden = editing && showHidden && showHidden.checked;
    var visible = cards.filter(function (card) {
      applyState(card);
      var id = idOf(card);
      if (prefs.hidden[id] && !withHidden) return false;
      if (cat === "__favorites" && !prefs.favorites[id]) return false;
      if (cat && cat !== "__favorites" && catOf(card) !== cat) return false;
      if (!q) return true;
      var name = card.getAttribute("data-name") || "", domain = card.getAttribute("data-domain") || "";
      var text = (name + " " + domain + " " + catOf(card)).toLowerCase();
      var op = searchOp ? searchOp.value : "contains";
      if (op === "notContains") return text.indexOf(q) === -1;
      if (op === "equals") return name === q || domain === q;
      if (op === "startsWith") return name.indexOf(q) === 0 || domain.indexOf(q) === 0;
      return text.indexOf(q) !== -1;
    });
    visible.sort(compare);
    var groups = {};
    visible.forEach(function (card) {
      var name = cat === "__favorites" ? "Favorites" : catOf(card);
      (groups[name] = groups[name] || []).push(card);
    });
    var names = Object.keys(groups).sort(function (a, b) {
      var ai = ORDER.indexOf(a), bi = ORDER.indexOf(b);
      ai = ai === -1 ? 999 : ai; bi = bi === -1 ? 999 : bi;
      return ai === bi ? a.localeCompare(b) : ai - bi;
    });
    gallery.textContent = "";
    names.forEach(function (name, i) { gallery.appendChild(section(name, groups[name], i + 1)); });
    gallery.setAttribute("data-view", prefs.view);
    gallery.setAttribute("data-density", prefs.density);
    gallery.style.setProperty("--gallery-card-min", prefs.width + "px");
    if (countEl) countEl.textContent = String(visible.length);
    if (emptyMsg) emptyMsg.hidden = visible.length !== 0;
    document.querySelectorAll("[data-gallery-view]").forEach(function (b) {
      var on = b.getAttribute("data-gallery-view") === prefs.view;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    document.querySelectorAll("[data-category-pill]").forEach(function (p) {
      var on = p.getAttribute("data-category-pill") === cat;
      p.classList.toggle("active", on);
      p.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  cards.forEach(function (card) {
    var img = card.querySelector(".app-icon");
    if (img) {
      img.addEventListener("error", function () { card.classList.add("icon-failed"); });
      if (img.complete && !img.naturalWidth) card.classList.add("icon-failed");
    }
    var star = card.querySelector(".app-favorite");
    if (star) star.addEventListener("click", function () {
      var id = idOf(card);
      if (prefs.favorites[id]) delete prefs.favorites[id]; else prefs.favorites[id] = true;
      commit();
    });
    var catSel = card.querySelector(".gallery-card-category");
    if (catSel) catSel.addEventListener("change", function () { prefs.categories[idOf(card)] = catSel.value; commit(); });
    var hiddenCb = card.querySelector(".gallery-card-hidden");
    if (hiddenCb) hiddenCb.addEventListener("change", function () {
      if (hiddenCb.checked) prefs.hidden[idOf(card)] = true; else delete prefs.hidden[idOf(card)];
      commit();
    });
    card.addEventListener("dragstart", function (e) {
      if (!editing) return;
      dragged = card;
      card.classList.add("is-dragging");
      // Firefox only starts a drag when dataTransfer carries something.
      try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", idOf(card)); } catch (err) {}
    });
    card.addEventListener("dragend", function () {
      card.classList.remove("is-dragging");
      dragged = null;
      prefs.order = Array.prototype.map.call(gallery.querySelectorAll(".app-launch-card"), idOf);
      prefs.sort = "custom";
      if (sortSel) sortSel.value = "custom";
      commit();
    });
  });
  gallery.addEventListener("dragover", function (e) {
    if (!dragged) return;
    e.preventDefault();
    var target = e.target.closest(".app-launch-card");
    if (!target || target === dragged || target.parentNode !== dragged.parentNode) return;
    var r = target.getBoundingClientRect();
    target.parentNode.insertBefore(dragged, e.clientY < r.top + r.height / 2 ? target : target.nextSibling);
  });
  gallery.addEventListener("click", function (e) {
    var toggle = e.target.closest(".gallery-section-toggle");
    if (!toggle) return;
    var grid = $(toggle.getAttribute("aria-controls"));
    var name = toggle.closest(".gallery-section").getAttribute("data-gallery-section");
    var open = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", open ? "false" : "true");
    if (grid) grid.hidden = open;
    if (open) collapsed[name] = true; else delete collapsed[name];
  });
  var searchTimer = 0;
  if (search) search.addEventListener("input", function () { clearTimeout(searchTimer); searchTimer = setTimeout(render, 80); });
  if (searchOp) searchOp.addEventListener("change", render);
  if (category) category.addEventListener("change", render);
  document.querySelectorAll("[data-category-pill]").forEach(function (pill) {
    pill.addEventListener("click", function () {
      if (category) category.value = pill.getAttribute("data-category-pill");
      render();
    });
  });
  if (sortSel) sortSel.addEventListener("change", function () { prefs.sort = sortSel.value; commit(); });
  document.querySelectorAll("[data-gallery-view]").forEach(function (b) {
    b.addEventListener("click", function () { prefs.view = b.getAttribute("data-gallery-view"); commit(); });
  });
  // Dragging the slider only moves a CSS variable; the choice is saved once.
  if (widthIn) widthIn.addEventListener("input", function () {
    prefs.width = Number(widthIn.value);
    if (widthOut) widthOut.textContent = prefs.width + "px";
    gallery.style.setProperty("--gallery-card-min", prefs.width + "px");
  });
  if (widthIn) widthIn.addEventListener("change", save);
  if (densitySel) densitySel.addEventListener("change", function () { prefs.density = densitySel.value; commit(); });
  if (layoutToggle) layoutToggle.addEventListener("click", function () {
    editing = !editing;
    if (layoutPanel) layoutPanel.hidden = !editing;
    layoutToggle.setAttribute("aria-expanded", editing ? "true" : "false");
    layoutToggle.textContent = editing ? "Done" : "Customize";
    gallery.classList.toggle("is-customizing", editing);
    render();
  });
  if (showHidden) showHidden.addEventListener("change", render);
  if (resetBtn) resetBtn.addEventListener("click", function () {
    if (!window.confirm("Reset this browser’s app gallery layout and card preferences?")) return;
    prefs = fresh();
    save();
    if (search) search.value = "";
    if (category) category.value = "";
    if (showHidden) showHidden.checked = false;
    sync();
    render();
  });
  function sync() {
    if (sortSel) sortSel.value = prefs.sort;
    if (widthIn) widthIn.value = String(prefs.width);
    if (widthOut) widthOut.textContent = prefs.width + "px";
    if (densitySel) densitySel.value = prefs.density;
  }
  sync();
  render();
})();

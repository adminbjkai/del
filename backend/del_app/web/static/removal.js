/* DEL removal pages: plan builder preset, the live-execute gate, and job
   progress polling. Loaded only by plan.html and job_detail.html. */
(function () {
  "use strict";
  var DEL = window.DEL || {};
  var escapeHtml = DEL.util.escapeHtml;
  function $(id) { return document.getElementById(id); }
  // Same wording as formatting._seconds on the server.
  function seconds(v) {
    var s = Number(v);
    if (v === null || v === undefined || v === "" || !isFinite(s) || s < 0) return "—";
    if (s < 1) return Math.round(s * 1000) + " ms";
    if (s < 60) return s.toFixed(1) + "s";
    return (s / 60).toFixed(1) + "m";
  }

  // --- Plan builder: "Complete removal (everything)" ticks every option ----
  var preset = $("preset-complete-removal");
  if (preset) {
    preset.addEventListener("change", function () {
      if (!preset.checked) return;
      ["remove-named-volumes", "remove-bind-data", "remove-repo", "remove-networks"].forEach(function (id) {
        if ($(id)) $(id).checked = true;
      });
      document.querySelectorAll(".approved-volume-checkbox").forEach(function (cb) { cb.checked = true; });
      if ($("remove_images")) $("remove_images").value = "exclusive";
      if ($("backup")) $("backup").value = "none";
    });
  }

  // --- Execute: dry run is the calm default; live shows the hazard zone and,
  //     when named volumes would be deleted, requires typing "y". ----------
  var form = $("execute-form");
  if (form) {
    var modeLive = $("mode-live");
    var liveBox = $("live-confirm-box");
    var phrase = $("confirm-phrase");
    var btn = $("execute-btn");
    var panel = form.closest(".execute-panel");
    var REQUIRED = "y";
    var refresh = function () {
      var live = !!(modeLive && modeLive.checked);
      if (liveBox) liveBox.hidden = !live;
      if (panel) panel.classList.toggle("is-live-mode", live);
      btn.disabled = live && !!phrase && phrase.value !== REQUIRED;
      btn.classList.toggle("btn-primary", !live);
      btn.classList.toggle("btn-danger", live);
      var label = btn.getAttribute(live ? "data-label-live" : "data-label-dry");
      if (label) btn.textContent = label;
    };
    form.querySelectorAll('input[name="mode"]').forEach(function (r) { r.addEventListener("change", refresh); });
    if (phrase) phrase.addEventListener("input", refresh);
    form.addEventListener("submit", function (evt) {
      var live = !!(modeLive && modeLive.checked);
      if (live && phrase && phrase.value !== REQUIRED) {
        evt.preventDefault();
        window.alert('Type "' + REQUIRED + '" to confirm live volume deletion.');
        return;
      }
      if (live && !window.confirm("Run this removal plan LIVE now? Irreversible steps cannot be undone.")) {
        evt.preventDefault();
      }
    });
    refresh();
  }

  // --- Job detail: poll status, grow stage tables, progress, auto-scroll ---
  var output = $("job-output");
  if (!output) return;
  var jobId = output.getAttribute("data-job-id");
  var statusEl = $("job-status");
  var scrollToggle = $("autoscroll-toggle");
  var fill = $("job-progress-fill");
  var track = $("job-progress");
  var progressLabel = $("job-progress-label");
  var currentStep = $("job-current-step");
  var noSteps = $("job-no-steps");
  var TERMINAL = ["done", "failed", "success", "error", "refused"];
  var initial = statusEl ? statusEl.textContent.trim() : "";
  // A job that had already finished when the page loaded needs no polling.
  if (TERMINAL.indexOf(initial) !== -1) return;

  function stageTable(stage) {
    var table = output.querySelector('table[data-stage="' + stage.replace(/"/g, "") + '"]');
    if (table) return table;
    if (noSteps) { noSteps.remove(); noSteps = null; }
    var section = document.createElement("section");
    section.className = "panel";
    section.innerHTML =
      "<h2>" + escapeHtml(stage) + ' <span class="count-pill">0</span></h2>' +
      '<table class="table job-steps" data-stage="' + escapeHtml(stage) + '">' +
      '<caption class="sr-only">Steps in the ' + escapeHtml(stage) + " stage</caption>" +
      '<thead><tr><th scope="col">#</th><th scope="col">Operation</th><th scope="col">State</th>' +
      '<th scope="col">Exit</th><th scope="col">Duration</th><th scope="col">Output</th></tr></thead><tbody></tbody></table>';
    output.appendChild(section);
    return section.querySelector("table");
  }
  function stepRow(step) {
    var row = output.querySelector('tr[data-step-seq="' + step.seq + '"]');
    if (row) return row;
    var table = stageTable(step.stage || "steps");
    row = document.createElement("tr");
    row.setAttribute("data-step-seq", step.seq);
    row.innerHTML = "<td>" + escapeHtml(step.seq) + '</td><td class="mono">' + escapeHtml(step.operation || "") +
      '</td><td class="step-state"></td><td class="step-exit">—</td><td class="step-duration">—</td><td class="step-output">—</td>';
    table.tBodies[0].appendChild(row);
    var pill = table.closest("section").querySelector(".count-pill");
    if (pill) pill.textContent = String(table.tBodies[0].rows.length);
    return row;
  }
  function apply(data) {
    if (statusEl && data.status) {
      statusEl.textContent = data.status;
      statusEl.className = "badge status-" + data.status + (TERMINAL.indexOf(data.status) === -1 ? " is-live" : "");
    }
    (data.steps || []).forEach(function (step) {
      var row = stepRow(step);
      row.querySelector(".step-state").innerHTML = '<span class="badge status-' + escapeHtml(step.state) +
        (step.state === "running" ? " is-live" : "") + '">' + escapeHtml(step.state) + "</span>";
      row.querySelector(".step-exit").textContent = step.exit_code == null ? "—" : String(step.exit_code);
      row.querySelector(".step-duration").textContent = seconds(step.duration);
      row.querySelector(".step-output").innerHTML = step.output_sanitized
        ? '<details><summary>output</summary><pre class="output">' + escapeHtml(step.output_sanitized) + "</pre></details>"
        : "—";
    });
    if (fill && data.progress) {
      var pct = data.progress.pct || 0;
      fill.style.width = pct + "%";
      if (track) track.setAttribute("aria-valuenow", String(pct));
      if (progressLabel) progressLabel.textContent = data.progress.done + " of " + data.progress.total + " steps done (" + pct + "%)";
    }
    if (currentStep) {
      currentStep.textContent = data.current_step
        ? "Running: " + (data.current_step.stage || "") + ", " + (data.current_step.operation || "") : "";
    }
    if (scrollToggle && scrollToggle.checked) {
      var last = output.lastElementChild;
      if (last) last.scrollIntoView({ block: "end", behavior: "smooth" });
    }
    if (TERMINAL.indexOf(data.status) !== -1 && DEL.toast) {
      DEL.toast("Job " + data.status, data.status === "done" || data.status === "success" ? "ok" : "error");
    }
  }

  // Backoff 2s to 10s; paused while the tab is hidden.
  var delay = 2000, timer = null, stopped = false;
  function schedule() {
    if (stopped || document.hidden) return;
    timer = setTimeout(poll, delay);
    delay = Math.min(delay * 1.5, 10000);
  }
  function poll() {
    timer = null;
    fetch("/jobs/" + jobId + "/status", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        apply(data || {});
        if (data && TERMINAL.indexOf(data.status) !== -1) stopped = true;
        else schedule();
      })
      .catch(schedule);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !stopped && !timer) { delay = 2000; poll(); }
    if (document.hidden && timer) { clearTimeout(timer); timer = null; }
  });
  timer = setTimeout(poll, delay);
})();

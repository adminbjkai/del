/* Runs in <head> before first paint: theme (and table density) from
   localStorage, else the OS colour-scheme preference. */
(function () {
  try {
    var t = localStorage.getItem("del.theme");
    if (t !== "dark" && t !== "light") {
      t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", t);
    document.querySelectorAll("meta[data-theme-color]").forEach(function (meta) {
      meta.content = t === "light" ? "#edf1f4" : "#0f1f30";
    });
    var d = localStorage.getItem("del-density");
    if (d === "compact" || d === "comfortable") document.documentElement.setAttribute("data-density", d);
  } catch (e) {}
})();

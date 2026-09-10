(function () {
  try {
    var t = localStorage.getItem("del.theme");
    if (t !== "dark" && t !== "light") {
      t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", t);
    var d = localStorage.getItem("del-density");
    if (d === "compact" || d === "comfortable") {
      document.documentElement.setAttribute("data-density", d);
    }
  } catch (e) {}
})();

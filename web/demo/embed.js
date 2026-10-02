// Demo pages only: inject the widget's one-tag embed using the git-ignored local config
// (written by `python -m app.cli seed-demo --demo-config`). A real site would simply include:
// <script src="https://api.example/widget.js" data-key="bap_widget_..." async></script>
(function () {
  var site = document.currentScript && document.currentScript.dataset.tenant;
  var config = window.BAP_DEMO;
  var key = config && config.keys && config.keys[site];
  if (!key) {
    var note = document.createElement("div");
    note.className = "demo-setup-note";
    note.textContent =
      "Demo setup needed: run ./scripts/demo-setup.sh from the repository root, then reload.";
    document.body.appendChild(note);
    return;
  }
  var script = document.createElement("script");
  script.src = config.api.replace(/\/+$/, "") + "/widget.js";
  script.async = true;
  script.dataset.key = key;
  script.dataset.api = config.api;
  document.body.appendChild(script);
})();

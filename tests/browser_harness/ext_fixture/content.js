(function () {
  function markPage() {
    if (document.documentElement) {
      document.documentElement.setAttribute("data-fixture-extension", "loaded");
    }
    if (document.body) {
      document.body.setAttribute("data-fixture-extension", "loaded");
    }
  }

  markPage();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", markPage);
  }
})();

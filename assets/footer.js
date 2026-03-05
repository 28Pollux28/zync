document.addEventListener("DOMContentLoaded", function () {
  var footer = document.querySelector("footer.footer .container");
  if (!footer) return;

  var el = document.createElement("div");
  el.className = "text-muted";
  el.style.cssText = "margin-top: 8px; font-size: 0.7em;";
  el.innerHTML =
    'Powered by ' +
    '<a href="https://github.com/28Pollux28/galvanize" target="_blank" rel="noopener" class="text-secondary">Galvanize</a>' +
    ' + ' +
    '<a href="https://github.com/28Pollux28/zync" target="_blank" rel="noopener" class="text-secondary">Zync</a>';
  footer.appendChild(el);
});

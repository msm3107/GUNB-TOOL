"use strict";
(() => {
  const token = window.location.hash.slice(1);
  if (!token) return;
  // Remove the capability from history before any user interaction.
  window.history.replaceState(null, "", window.location.pathname);
  const field = document.getElementById("magic-token");
  if (field && /^v1\.[0-9]{1,12}\.[A-Za-z0-9_-]{43}\.[0-9a-f]{64}$/.test(token)) {
    field.value = token;
    document.getElementById("magic-code-field").hidden = true;
  }
  // Explicit form submission is required; scanners cannot consume a GET link.
})();

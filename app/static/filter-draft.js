"use strict";
(() => {
  const key = "zt:filter-draft:v1";
  const pending = "zt:filter-draft:logout";
  const fields = ["wojewodztwo", "branza", "powiaty", "kategorie", "lat", "lon", "promien_km"];
  const storage = (action) => { try { return action(sessionStorage); } catch { return undefined; } };
  function valid(value) {
    return value && typeof value === "object" && Object.keys(value).length === fields.length &&
      fields.every(k => Object.hasOwn(value, k)) &&
      typeof value.wojewodztwo === "string" && /^\d{2}$/.test(value.wojewodztwo) &&
      typeof value.branza === "string" && value.branza.length <= 30 &&
      ["powiaty", "kategorie"].every(k => Array.isArray(value[k]) && value[k].length <= (k === "powiaty" ? 20 : 7) &&
        value[k].every(v => typeof v === "string" && v.length <= 100) && new Set(value[k]).size === value[k].length) &&
      ["lat", "lon", "promien_km"].every(k => value[k] === null || (typeof value[k] === "number" && Number.isFinite(value[k])));
  }
  window.FilterDraft = Object.freeze({
    load() {
      const raw = storage(s => s.getItem(key));
      if (!raw) return { state: "none" };
      try {
        if (raw.length > 8192) throw new Error();
        const draft = JSON.parse(raw);
        if (draft.version !== 1 || !valid(draft.filters)) throw new Error();
        return { state: "ready", filters: draft.filters };
      } catch { return { state: "invalid" }; }
    },
    save(filters) {
      if (!valid(filters)) return false;
      return storage(s => { s.setItem(key, JSON.stringify({ version: 1, filters })); return true; }) === true;
    },
  });
  // Clear only after the server confirms an anonymous GET following logout.
  if (document.getElementById("filter-draft-signed-out") && storage(s => s.getItem(pending))) {
    storage(s => { s.removeItem(key); s.removeItem(pending); });
  }
  document.addEventListener("submit", event => {
    const form = event.target;
    if (form instanceof HTMLFormElement && new URL(form.action).origin === location.origin &&
        new URL(form.action).pathname === "/auth/logout") storage(s => s.setItem(pending, "1"));
  });
})();

"use strict";

(() => {
  const form = document.getElementById("package-form");
  if (!form) return;
  const region = form.elements.namedItem("wojewodztwo");
  const counties = form.elements.namedItem("powiaty");
  const status = document.getElementById("estimate-status");
  const result = document.getElementById("estimate-result");
  let sequence = 0;
  let controller;
  let timer;
  let started = form.dataset.ready === "true";

  function filterCounties() {
    for (const option of counties.options) {
      const allowed = option.dataset.region === region.value;
      option.hidden = !allowed;
      option.disabled = !allowed;
      if (!allowed) option.selected = false;
    }
  }

  function invalidate() {
    sequence += 1;
    controller?.abort();
    clearTimeout(timer);
    form.removeAttribute("aria-busy");
    result.hidden = true;
    status.textContent = "Ustawienia zmienione. Wynik wymaga ponownego przeliczenia.";
    return sequence;
  }

  async function estimate(scroll = false) {
    const current = invalidate();
    if (!form.reportValidity()) return;
    started = true;
    const data = new FormData(form);
    const payload = {
      wojewodztwo: data.get("wojewodztwo"),
      branza: data.get("branza"),
      powiaty: data.getAll("powiaty"),
      kategorie: data.getAll("kategorie"),
    };
    for (const key of ["lat", "lon", "promien_km"]) {
      const value = data.get(key).trim();
      if (value !== "") payload[key] = Number(value);
    }
    controller = new AbortController();
    status.textContent = "Liczenie inwestycji…";
    form.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(form.dataset.estimateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
        credentials: "same-origin",
      });
      const data = await response.json();
      if (current !== sequence) return;
      if (!response.ok) throw new Error(data.detail || "Nie udało się policzyć inwestycji.");
      document.getElementById("estimate-count").textContent = data.count;
      document.getElementById("estimate-located").textContent = data.with_parcel_coordinates;
      document.getElementById("estimate-unlocated").textContent = data.without_parcel_coordinates;
      document.getElementById("estimate-observed").textContent = data.last_seen
        ? "Ostatnia obserwacja pasujących danych: " + data.last_seen.slice(0, 10) : "";
      result.hidden = false;
      status.textContent = data.count === 0 ? "Brak pasujących spraw w zaimportowanych danych." : "Liczba pasujących inwestycji: " + data.count + ".";
    } catch (error) {
      if (current === sequence && error.name !== "AbortError") {
        status.textContent = error instanceof TypeError
          ? "Nie udało się połączyć z estymatorem. Spróbuj ponownie." : error.message;
      }
    } finally {
      if (current === sequence) {
        form.removeAttribute("aria-busy");
        if (scroll) status.scrollIntoView({ block: "nearest" });
      }
    }
  }

  form.addEventListener("submit", (event) => { event.preventDefault(); estimate(true); });
  form.addEventListener("input", () => {
    filterCounties();
    invalidate();
    if (started) timer = setTimeout(estimate, 400);
  });
  filterCounties();
  if (started) estimate();
})();

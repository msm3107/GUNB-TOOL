"use strict";
(() => {
  const form = document.getElementById("package-form");
  if (!form) return;
  const box = document.getElementById("place-fields");
  const query = document.getElementById("place_query");
  const choice = document.getElementById("place_id");
  const version = document.getElementById("place_version");
  const region = document.getElementById("wojewodztwo");
  const status = document.getElementById("place-status");
  let sequence = 0, controller, deadline;
  function cancel() {
    sequence += 1; controller?.abort(); clearTimeout(deadline);
    box.removeAttribute("aria-busy"); return sequence;
  }
  function reset() {
    choice.replaceChildren(new Option("Wybierz miejscowość po wyszukaniu", ""));
    version.value = "";
  }
  function changed() { form.dispatchEvent(new Event("input", { bubbles: true })); }
  form.addEventListener("input", event => {
    if (event.target === query) {
      cancel(); reset(); status.textContent = "Nazwa zmieniona. Wyszukaj i wybierz konkretną miejscowość.";
    } else if (event.target === choice) {
      cancel();
      const selected = choice.selectedOptions[0];
      version.value = selected?.dataset.version || "";
      if (choice.value) query.value = selected.dataset.name || query.value;
      status.textContent = choice.value ? "Wybrano: " + selected.textContent + ". Punkt miejscowości, nie adres firmy." : "Nie wybrano miejscowości. Wybierz wynik albo jawnie usuń wyszukiwanie.";
    } else if (event.target === region) {
      cancel(); status.textContent = "Województwo zmienione. Zachowano wybrany punkt; nowe wyszukiwanie użyje nowego województwa.";
    }
  });
  document.getElementById("place-clear").addEventListener("click", event => {
    event.preventDefault();
    cancel(); query.value = ""; reset();
    status.textContent = "Usunięto wybór miejscowości. Promień i pozostałe filtry zachowano."; changed();
  });
  form.addEventListener("submit", async event => {
    const action = event.submitter;
    if (action?.name !== "place_action" ||
        !(action.value === "search" || (action.value === "auto" && document.activeElement === query))) return;
    event.preventDefault(); event.stopImmediatePropagation();
    const current = cancel(); reset(); changed();
    if (query.value.trim().length < 2 || query.value.trim().length > 80) {
      status.textContent = "Wpisz od 2 do 80 znaków nazwy miejscowości."; return;
    }
    const request = new AbortController(); controller = request;
    let timedOut = false;
    deadline = setTimeout(() => {
      if (sequence !== current) return;
      timedOut = true; request.abort(); box.removeAttribute("aria-busy");
      status.textContent = "Przekroczono czas wyszukiwania (10 s). Zachowano filtry; ponów ręcznie.";
    }, 10000);
    const ownDeadline = deadline;
    status.textContent = "Szukanie miejscowości…"; box.setAttribute("aria-busy", "true");
    try {
      const params = new URLSearchParams({ q: query.value.trim(), region: region.value });
      const response = await fetch(box.dataset.searchUrl + "?" + params, {signal: request.signal, credentials: "same-origin"});
      const data = await response.json();
      if (sequence !== current || timedOut) return;
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Nie udało się wyszukać miejscowości.");
      if (!Array.isArray(data.places) || data.places.length > 20 ||
          !data.places.every(p => p && ["id","name","municipality","county","region","source_version"].every(k => typeof p[k] === "string" && p[k].length > 0 && p[k].length <= 200) && p.id.length <= 100 && p.source_version.length <= 128) ||
          new Set(data.places.map(p => p.id)).size !== data.places.length) throw new Error("Niepoprawna odpowiedź katalogu. Ponów ręcznie.");
      for (const place of data.places) {
        const option = new Option(`${place.name} — ${place.municipality}, ${place.county} (woj. ${place.region})`, place.id);
        option.dataset.name = place.name; option.dataset.version = place.source_version;
        choice.append(option);
      }
      status.textContent = data.places.length ? "Wybierz konkretną miejscowość z listy; żaden punkt nie został wybrany automatycznie." : "Brak miejscowości pasujących do nazwy i województwa.";
    } catch (error) {
      if (sequence === current && !timedOut && error.name !== "AbortError") status.textContent = error instanceof TypeError ? "Katalog jest niedostępny. Filtry zachowano; ponów ręcznie." : error.message;
    } finally {
      clearTimeout(ownDeadline);
      if (sequence === current) box.removeAttribute("aria-busy");
    }
  });
  window.addEventListener("pagehide", cancel);
})();

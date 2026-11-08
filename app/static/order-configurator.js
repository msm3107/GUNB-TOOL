"use strict";
(() => {
  const form = document.getElementById("package-form");
  if (!form) return;
  const region = form.elements.namedItem("wojewodztwo");
  const countyBox = document.getElementById("county-options");
  const search = document.getElementById("county-search");
  const status = document.getElementById("estimate-status");
  const mobile = document.getElementById("estimate-mobile");
  const result = document.getElementById("estimate-result");
  const zero = document.getElementById("estimate-zero");
  const summary = document.getElementById("filter-errors");
  const draftStatus = document.getElementById("draft-status");
  const allowedFields = ["wojewodztwo", "branza", "powiaty", "kategorie", "lat", "lon", "promien_km", "place_query", "place_id"];
  let sequence = 0, controller, timer;
  let started = form.dataset.ready === "true";
  const counties = () => [...countyBox.querySelectorAll("input[name=powiaty]")];
  function message(text) { status.textContent = mobile.textContent = text; }
  function errors(detail = "", fields = {}) {
    summary.replaceChildren();
    summary.hidden = !detail;
    const title = document.createElement("p"); title.textContent = detail; summary.append(title);
    const list = document.createElement("ul"); summary.append(list);
    for (const key of allowedFields) {
      const node = document.getElementById("error-" + key);
      const control = document.getElementById(key);
      const text = typeof fields[key] === "string" ? fields[key] : "";
      node.textContent = text; node.hidden = !text;
      control.setAttribute("aria-invalid", text ? "true" : "false");
      const described = (control.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
      if (!described.includes(node.id)) described.push(node.id);
      control.setAttribute("aria-describedby", described.join(" "));
      if (text) {
        const item = document.createElement("li"), link = document.createElement("a");
        link.href = "#" + key; link.textContent = text; item.append(link); list.append(item);
      }
    }
    if (Object.keys(fields).some(k => ["lat", "lon", "promien_km"].includes(k))) form.querySelector(".radius-options").open = true;
  }
  function filterCounties() {
    const query = search.value.trim().toLocaleLowerCase("pl");
    for (const input of counties()) {
      input.closest("label").hidden = !input.checked &&
        (input.dataset.region !== region.value || !input.closest("label").textContent.toLocaleLowerCase("pl").includes(query));
    }
    document.getElementById("county-count").textContent = "Wybrano powiatów: " + counties().filter(n => n.checked).length + " / 20";
  }
  function payload() {
    const data = new FormData(form);
    const value = { wojewodztwo: data.get("wojewodztwo"), branza: data.get("branza"),
      powiaty: data.getAll("powiaty"), kategorie: data.getAll("kategorie") };
    for (const key of ["lat", "lon", "promien_km"]) value[key] = data.get(key).trim() === "" ? null : Number(data.get(key));
    if (["place_query", "place_id", "place_version"].some(k => data.get(k))) {
      value.place_query = data.get("place_query"); value.place_id = data.get("place_id") || null; value.place_version = data.get("place_version") || null;
    }
    return value;
  }
  function scope() {
    const selected = counties().filter(n => n.checked).map(n => n.closest("label").textContent.trim());
    const categories = [...form.querySelectorAll('input[name=kategorie]:checked')].map(n => n.closest('label').textContent.trim());
    const coordinates = ['lat','lon','promien_km'].map(k => form.elements.namedItem(k).value || '—');
    document.getElementById("estimate-scope").textContent = "Zakres: " + region.selectedOptions[0].textContent +
      "; powiaty: " + (selected.join(", ") || "całe województwo") + "; branża: " + form.elements.branza.selectedOptions[0].textContent +
      "; typy: " + categories.join(', ') + "; punkt N/E/promień km: " + coordinates.join('/') +
      "; miejscowość: " + (form.elements.place_id.value ? form.elements.place_id.selectedOptions[0].textContent : "nie wybrano");
  }
  function invalidate() {
    sequence += 1; controller?.abort(); clearTimeout(timer);
    form.removeAttribute("aria-busy"); result.hidden = zero.hidden = true;
    message("Ustawienia zmienione. Wynik wymaga ponownego przeliczenia."); scope();
    return sequence;
  }
  function save() {
    if (['lat','lon','promien_km'].some(k => {
      const input = form.elements.namedItem(k);
      return input.validity.badInput || (input.value.trim() !== '' && !Number.isFinite(Number(input.value)));
    })) {
      draftStatus.textContent = 'Nie zapisano niepoprawnej liczby w szkicu. Popraw pole; poprzedni szkic zachowano.'; return;
    }
    if (window.FilterDraft && !window.FilterDraft.save(payload())) draftStatus.textContent = "Szkic jest niedostępny w tej przeglądarce. Obliczenia nadal działają.";
  }
  async function estimate(scroll = false) {
    const current = invalidate(); errors();
    if (!form.reportValidity()) { message("Sprawdź pola formularza."); return; }
    const badNumbers = {};
    for (const key of ['lat','lon','promien_km']) {
      const value = form.elements.namedItem(key).value.trim();
      if (value !== '' && !Number.isFinite(Number(value))) badNumbers[key] = 'Podaj poprawną, skończoną liczbę.';
    }
    if (Object.keys(badNumbers).length) { errors('Sprawdź współrzędne i promień.',badNumbers); message('Popraw liczby przed obliczeniem.'); return; }
    if (counties().some(n => n.checked && n.dataset.region !== region.value)) {
      const text = "Wybrane powiaty należą do innego województwa. Popraw lub jawnie wyczyść wybór.";
      errors(text, { powiaty: text }); message(text); return;
    }
    started = true; save();
    const request = new AbortController(); controller = request;
    let timedOut = false;
    const deadline = setTimeout(() => {
      if (current !== sequence) return;
      timedOut = true; request.abort(); form.removeAttribute("aria-busy");
      message("Przekroczono czas obliczenia (10 s). Filtry zachowano. Ponów ręcznie.");
    }, 10000);
    message("Liczenie inwestycji…"); form.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(form.dataset.estimateUrl, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload()), signal: request.signal, credentials: "same-origin" });
      const data = await response.json();
      if (current !== sequence || timedOut) return;
      if (!response.ok) { errors(data.detail || "Nie udało się obliczyć wyniku.", data.field_errors || {}); throw new Error(data.detail || "Nie udało się obliczyć wyniku."); }
      if (!["count", "with_parcel_coordinates", "without_parcel_coordinates"].every(k => Number.isSafeInteger(data[k]) && data[k] >= 0) ||
          !(data.last_seen === null || typeof data.last_seen === "string")) throw new Error("Niepoprawna odpowiedź estymatora. Ponów ręcznie.");
      document.getElementById("estimate-count").textContent = data.count;
      document.getElementById("estimate-located").textContent = data.with_parcel_coordinates;
      document.getElementById("estimate-unlocated").textContent = data.without_parcel_coordinates;
      document.getElementById("estimate-observed").textContent = data.last_seen ? "Ostatnia obserwacja pasujących danych: " + data.last_seen.slice(0, 10) : "";
      result.hidden = false; zero.hidden = data.count !== 0;
      message(data.count === 0 ? "Brak pasujących spraw w dostępnych danych. Sprawdź zakres poniżej; brak gwarancji kompletności importu." : "Liczba pasujących inwestycji: " + data.count + ".");
    } catch (error) {
      if (current === sequence && !timedOut && error.name !== "AbortError") message(error instanceof TypeError ? "Nie udało się połączyć z estymatorem. Filtry zachowano; ponów ręcznie." : error.message);
    } finally {
      clearTimeout(deadline);
      if (current === sequence) { form.removeAttribute("aria-busy"); if (scroll) (getComputedStyle(mobile).display === 'none' ? status : mobile).scrollIntoView({ block: "nearest" }); }
    }
  }
  function restore() {
    if (form.dataset.native === "true") return true;
    const draft = window.FilterDraft?.load();
    if (!draft || draft.state === "none") return true;
    if (draft.state === "invalid") { draftStatus.textContent = "Nie przywrócono nieprawidłowego szkicu. Ustaw filtry i przelicz ręcznie."; return false; }
    const value = draft.filters;
    const hasOption = (name, v) => [...form.elements.namedItem(name).options].some(o => o.value === v);
    const categoryValues = [...form.querySelectorAll("input[name=kategorie]")].map(n => n.value);
    if (!hasOption("wojewodztwo", value.wojewodztwo) || !hasOption("branza", value.branza) ||
        value.kategorie.some(v => !categoryValues.includes(v)) || value.powiaty.some(v => !/^\d{4}$/.test(v))) {
      draftStatus.textContent = "Szkic wymaga ponownego wyboru filtrów. Niczego nie przeliczono automatycznie."; return false;
    }
    region.value = value.wojewodztwo; form.elements.branza.value = value.branza;
    for (const code of value.powiaty) if (!counties().some(n => n.value === code)) {
      const label = document.createElement("label"), input = document.createElement("input");
      input.type = "checkbox"; input.name = "powiaty"; input.value = code; input.dataset.region = code.slice(0, 2);
      label.dataset.countyLabel = ""; label.append(input, document.createTextNode("Powiat " + code + " — zachowany wybór ze szkicu")); countyBox.append(label);
    }
    for (const input of counties()) input.checked = value.powiaty.includes(input.value);
    for (const input of form.querySelectorAll("input[name=kategorie]")) input.checked = value.kategorie.includes(input.value);
    if (Object.hasOwn(value, "place_id")) {
      form.elements.place_query.value = value.place_query;
      const choice = form.elements.place_id;
      if (value.place_id && ![...choice.options].some(n => n.value === value.place_id)) {
        const option = new Option(value.place_query + " — zachowany wybór; wymaga weryfikacji katalogu", value.place_id);
        option.dataset.name = value.place_query; option.dataset.version = value.place_version || ""; choice.append(option);
      }
      choice.value = value.place_id || ""; form.elements.place_version.value = value.place_version || "";
    }
    for (const key of ["lat", "lon", "promien_km"]) form.elements.namedItem(key).value = value[key] ?? "";
    if (value.lat !== null || value.lon !== null || value.promien_km !== null) form.querySelector(".radius-options").open = true;
    draftStatus.textContent = "Przywrócono dokładny zakres szkicu. Sprawdź wynik i ewentualne błędy pól.";
    return true;
  }
  const canStart = restore(); filterCounties(); scope(); document.getElementById("county-tools").hidden = false;
  // Save successful native actions, including clear; rejected input keeps the old draft.
  if (form.dataset.native === "true" && summary.hidden) save();
  search.addEventListener("input", filterCounties);
  for (const id of ["county-clear", "county-whole"]) document.getElementById(id).addEventListener("click", () => {
    for (const input of counties()) input.checked = false;
    filterCounties(); invalidate(); errors(); save(); if (started) timer = setTimeout(estimate, 400);
  });
  form.addEventListener("submit", event => {
    if (event.defaultPrevented) return;
    const action = event.submitter;
    const isEstimate = !action || action.id === "estimate-retry" ||
      (action.name === "place_action" && (action.value === "estimate" ||
        (action.value === "auto" && document.activeElement !== form.elements.place_query)));
    if (!isEstimate) { invalidate(); return; }
    event.preventDefault(); estimate(true);
  });
  form.addEventListener("input", event => {
    if (event.target === search) return;
    filterCounties(); invalidate(); errors(); save(); if (started) timer = setTimeout(estimate, 400);
  });
  window.addEventListener("pagehide", () => { controller?.abort(); clearTimeout(timer); });
  if (started && canStart) estimate(); else { result.hidden = zero.hidden = true; }
})();

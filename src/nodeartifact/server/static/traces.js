(() => {
  const results = document.getElementById("trace-results");
  const field = document.getElementById("trace-search");
  if (!results || !field || !window.EventSource) return;
  const form = field.closest("form");
  const address = new URL(results.dataset.liveUrl, window.location.href);
  const fragments = new URL(results.dataset.resultsUrl, window.location.href);
  let shown = address.searchParams.get("q") ?? "";
  let clocks = [];
  let source = null;
  let timer = null;
  let typing = null;
  let request = null;

  const duration = (ms) => (ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`);

  const count = () => {
    const now = performance.now();
    clocks = [...results.querySelectorAll("[data-elapsed-ms]")]
      .map((cell) => ({ cell, elapsed: Number(cell.dataset.elapsedMs), since: now }))
      .filter((clock) => Number.isFinite(clock.elapsed) && clock.elapsed >= 0);
  };

  const tick = () => {
    const now = performance.now();
    for (const clock of clocks) clock.cell.textContent = duration(clock.elapsed + now - clock.since);
  };

  const rowOf = (trace) => results.querySelector(`tr[data-trace="${trace}"]`);

  const merge = (body, fresh) => {
    const known = new Map([...body.querySelectorAll("tr[data-trace]")].map((row) => [row.dataset.trace, row]));
    const rows = [...fresh.querySelectorAll("tr[data-trace]")].map((row) => {
      const current = known.get(row.dataset.trace);
      if (current?.isEqualNode(row)) return current;
      current?.replaceWith(row);
      return row;
    });
    rows.forEach((row, index) => {
      if (body.children[index] !== row) body.insertBefore(row, body.children[index] ?? null);
    });
    while (body.children.length > rows.length) body.lastElementChild.remove();
  };

  const update = (html) => {
    const fresh = new DOMParser().parseFromString(html, "text/html").body;
    const focused = document.activeElement;
    const focusedTrace = results.contains(focused) ? focused.closest("tr[data-trace]")?.dataset.trace : null;
    const covered = Math.max(document.querySelector(".nodestep-topbar")?.getBoundingClientRect().bottom ?? 0, 0);
    const rows = [...results.querySelectorAll("tr[data-trace]")];
    const anchor = window.scrollY > 0 ? rows.find((row) => row.getBoundingClientRect().bottom > covered) : null;
    const anchorTrace = anchor?.dataset.trace;
    const anchorTop = anchor?.getBoundingClientRect().top ?? 0;
    const body = results.querySelector("tbody");
    const freshBody = fresh.querySelector("tbody");
    if (body && freshBody) {
      merge(body, freshBody);
      const table = body.closest(".nodestep-table-wrap");
      for (const child of [...results.children]) if (child !== table) child.remove();
      results.append(...[...fresh.children].filter((child) => !child.classList.contains("nodestep-table-wrap")));
    } else {
      results.replaceChildren(...fresh.childNodes);
    }
    if (focusedTrace && document.activeElement !== focused) {
      rowOf(focusedTrace)?.querySelector("a")?.focus({ preventScroll: true });
    }
    const moved = anchorTrace && rowOf(anchorTrace);
    if (moved) window.scrollBy(0, moved.getBoundingClientRect().top - anchorTop);
    count();
  };

  const follow = () => {
    if (!source) {
      source = new EventSource(address);
      source.addEventListener("traces", (event) => {
        if (event.lastEventId) address.searchParams.set("after", event.lastEventId);
        update(JSON.parse(event.data).html);
      });
    }
    if (!timer) timer = setInterval(tick, 1000);
  };

  const pause = () => {
    source?.close();
    source = null;
    clearInterval(timer);
    timer = null;
  };

  const search = async (text) => {
    clearTimeout(typing);
    typing = null;
    const query = text.split(/\s+/).filter(Boolean).join(" ");
    if (query === shown) return;
    shown = query;
    const page = new URL(window.location.href);
    if (query) page.searchParams.set("q", query);
    else page.searchParams.delete("q");
    window.history.replaceState(window.history.state, "", page);
    address.searchParams.set("q", query);
    fragments.searchParams.set("q", query);
    source?.close();
    source = null;
    request?.abort();
    const controller = new AbortController();
    request = controller;
    let html;
    try {
      const response = await fetch(fragments, { signal: controller.signal });
      if (!response.ok) throw new Error(`The search answered ${response.status}.`);
      html = await response.text();
    } catch {
      if (!controller.signal.aborted) window.location.assign(page);
      return;
    }
    update(html);
    if (!document.hidden) follow();
  };

  field.addEventListener("input", () => {
    clearTimeout(typing);
    typing = setTimeout(() => search(field.value), 250);
  });
  field.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    if (event.isComposing) return;
    if (!field.value) field.blur();
    field.value = "";
    search("");
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    search(field.value);
  });
  document.addEventListener("click", (event) => {
    const clear = event.target.closest("[data-clear-search]");
    if (!clear) return;
    event.preventDefault();
    field.value = "";
    search("");
    if (form.contains(clear)) field.focus();
    else document.getElementById("content")?.focus({ preventScroll: true });
  });
  document.addEventListener("visibilitychange", () => (document.hidden ? pause() : follow()));
  window.addEventListener("pagehide", pause);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      clearTimeout(typing);
      typing = null;
      field.value = shown;
    }
    if (!document.hidden) follow();
  });
  count();
  if (field.value !== field.defaultValue) typing = setTimeout(() => search(field.value), 250);
  if (!document.hidden) follow();
})();

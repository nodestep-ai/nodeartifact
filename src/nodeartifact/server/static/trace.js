(() => {
  const root = document.getElementById("trace-graph");
  if (!root) return;
  const graph = JSON.parse(document.getElementById("trace-graph-data").textContent);
  const diagram = document.getElementById("trace-diagram");
  const list = document.getElementById("trace-step-list");
  const position = document.getElementById("trace-position");
  const controls = document.getElementById("trace-replay");
  const play = controls.querySelector('[data-replay="play"]');
  const live = root.dataset.live === "true";
  const marks = ["visited", "current", "paused", "failed", "stopped", "tool-error"];
  const clock = document.getElementById("trace-duration");
  const details = new Map();
  let detail = document.getElementById("trace-step-detail");
  let view = detail.querySelector('[data-view][aria-current="page"]')?.dataset.view ?? "update";
  let shown = Number(list.querySelector('[aria-current="true"]')?.dataset.step) || graph.steps.length;
  let detailStep = graph.steps.length ? shown : 0;
  let stepping = shown < graph.steps.length;
  let request = 0;
  let timer = null;
  let svg = null;
  if (detailStep) details.set(detailStep, Promise.resolve(detail.cloneNode(true)));

  const pattern = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  const nodeOf = (id) => {
    if (!svg) return null;
    const exact = new RegExp(`(^|-)flowchart-${pattern(id)}-\\d+$`);
    return (
      [...svg.querySelectorAll("g.node")].find(
        (node) => node.dataset.id === id || exact.test(node.id),
      ) ?? null
    );
  };

  const edgesOf = (from, to) => {
    if (!svg) return [];
    const edges = [...svg.querySelectorAll("path.flowchart-link")];
    if (edges.some((edge) => (edge.dataset.id ?? "").includes("-"))) {
      const named = new RegExp(`^${pattern(from)}-${pattern(to)}(-\\d+)?$`);
      return edges.filter((edge) => named.test(edge.dataset.id ?? ""));
    }
    const stored = new RegExp(`(^|-)L[_-]${pattern(from)}[_-]${pattern(to)}[_-]\\d+$`);
    return edges.filter((edge) => stored.test(edge.dataset.id ?? "") || stored.test(edge.id));
  };

  const label = (numbers) =>
    numbers.length > 3
      ? `${numbers[0]}, ${numbers[1]} … ${numbers[numbers.length - 1]}`
      : numbers.join(", ");

  const badge = (node, numbers) => {
    const box = node.getBBox();
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    group.classList.add("trace-step-badge");
    group.setAttribute("aria-hidden", "true");
    text.textContent = label(numbers);
    group.append(rect, text);
    node.append(group);
    const width = text.getComputedTextLength() + 10;
    const x = box.x + box.width - width / 2;
    const y = box.y - 9;
    rect.setAttribute("x", x);
    rect.setAttribute("y", y);
    rect.setAttribute("width", width);
    rect.setAttribute("height", 18);
    rect.setAttribute("rx", 9);
    text.setAttribute("x", x + width / 2);
    text.setAttribute("y", y + 13);
    text.setAttribute("text-anchor", "middle");
  };

  const paint = () => {
    if (!svg) return;
    svg.querySelectorAll(".trace-step-badge").forEach((item) => item.remove());
    svg.querySelectorAll("g.node").forEach((node) => node.classList.remove(...marks));
    svg.querySelectorAll(".edge-taken").forEach((edge) => edge.classList.remove("edge-taken"));
    const steps = graph.steps.slice(0, shown);
    const numbers = new Map();
    steps.forEach((step, index) => {
      const node = step.mermaid_id && nodeOf(step.mermaid_id);
      const last = index === steps.length - 1;
      if (node) {
        node.classList.add("visited");
        if (["paused", "failed", "stopped"].includes(step.status)) node.classList.add(step.status);
        if (step.failed_tools.length) node.classList.add("tool-error");
        if (last && stepping) node.classList.add("current");
        numbers.set(node, [...(numbers.get(node) ?? []), step.number]);
      }
      if (step.mermaid_id && (last || !stepping)) {
        step.entered_from.forEach((from) =>
          edgesOf(from, step.mermaid_id).forEach((edge) => edge.classList.add("edge-taken")),
        );
      }
    });
    if (!stepping && graph.finished) {
      graph.ended_from.forEach((from) =>
        edgesOf(from, "END").forEach((edge) => edge.classList.add("edge-taken")),
      );
    }
    if (!stepping && live && graph.current) nodeOf(graph.current)?.classList.add("current");
    numbers.forEach((values, node) => badge(node, values));
  };

  const item = (step) => {
    const entry = document.createElement("li");
    const link = document.createElement("a");
    const line = document.createElement("span");
    const number = document.createElement("span");
    const title = document.createElement("span");
    const time = document.createElement("span");
    link.className = "nodestep-panel-item";
    link.href = `${root.dataset.pageUrl}?step=${step.number}#trace-step-detail`;
    link.dataset.step = step.number;
    line.className = "nodestep-panel-item-line";
    number.className = "trace-step-number";
    number.textContent = step.number;
    title.className = "nodestep-panel-item-title";
    title.textContent = step.node_name;
    line.append(number, title);
    if (step.status !== "completed") {
      const mark = document.createElement("span");
      mark.className = `nodestep-badge nodestep-badge-${step.status}`;
      mark.textContent = step.status[0].toUpperCase() + step.status.slice(1);
      line.append(mark);
    }
    if (step.failed_tools.length) {
      const mark = document.createElement("span");
      mark.className = "nodestep-badge nodestep-badge-failed";
      mark.textContent = "Tool error";
      line.append(mark);
    }
    time.className = "nodestep-panel-item-time";
    time.textContent = step.duration;
    line.append(time);
    link.append(line);
    entry.append(link);
    return entry;
  };

  const query = () =>
    detail.querySelector("[data-view-panel]:not([hidden]) .nodestep-data-search")?.value ?? "";

  const choose = (name, text) => {
    const tabs = [...detail.querySelectorAll("[data-view]")];
    const target = tabs.find((tab) => tab.dataset.view === name) ?? tabs[0];
    if (!target) return;
    for (const tab of tabs) {
      if (tab === target) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    let search = null;
    for (const panel of detail.querySelectorAll("[data-view-panel]")) {
      panel.hidden = panel.dataset.viewPanel !== target.dataset.view;
      if (!panel.hidden) search = panel.querySelector(".nodestep-data-search");
    }
    if (search && text && search.value !== text) {
      search.value = text;
      search.dispatchEvent(new Event("input", { bubbles: true }));
    }
  };

  const fetchDetail = (number) => {
    if (!details.has(number)) {
      details.set(
        number,
        fetch(`${root.dataset.pageUrl}?step=${number}`, { headers: { Accept: "text/html" } })
          .then((response) => {
            if (!response.ok) throw new Error(`The page answered ${response.status}.`);
            return response.text();
          })
          .then((text) => {
            const fresh = new DOMParser().parseFromString(text, "text/html").getElementById("trace-step-detail");
            return fresh && document.importNode(fresh, true);
          }),
      );
      if (details.size > 8) details.delete(details.keys().next().value);
    }
    return details.get(number);
  };

  const focused = () => {
    const active = document.activeElement;
    if (!active || !detail.contains(active)) return null;
    if (active.matches(".nodestep-data-search")) return ".nodestep-data-search";
    const tab = active.closest("[data-view]");
    if (tab) return `[data-view="${tab.dataset.view}"]`;
    const action = active.closest("[data-nodestep-data-action]");
    if (action) return `[data-nodestep-data-action="${action.dataset.nodestepDataAction}"]`;
    return null;
  };

  const refocus = (selector) => {
    if (!selector) return;
    const panel = detail.querySelector("[data-view-panel]:not([hidden])");
    const target = panel?.querySelector(selector) ?? detail.querySelector(selector);
    if (!target) return;
    target.focus({ preventScroll: true });
    if (target.matches(".nodestep-data-search")) target.setSelectionRange(target.value.length, target.value.length);
  };

  const retitle = () => {
    const title = detail.querySelector("#trace-step-title");
    const step = graph.steps[detailStep - 1];
    if (title && step) title.textContent = `Step ${detailStep} of ${graph.steps.length}: ${step.node_name}`;
  };

  const load = (number, force) => {
    if (!number || (number === detailStep && !force)) return;
    const ticket = ++request;
    fetchDetail(number)
      .then((fresh) => {
        if (ticket !== request) return;
        if (!fresh) throw new Error("The page has no step details.");
        const text = query();
        const focus = focused();
        const next = fresh.cloneNode(true);
        detail.replaceWith(next);
        detail = next;
        detailStep = number;
        choose(view, text);
        refocus(focus);
      })
      .catch(() => {
        details.delete(number);
        if (ticket !== request) return;
        const note = document.createElement("p");
        note.className = "nodestep-message nodestep-message-warning";
        note.textContent = `The details of step ${number} could not be loaded. Reload the page to try again.`;
        detail.replaceChildren(note);
        detail.hidden = false;
        detailStep = 0;
      });
  };

  const describe = (force) => {
    const total = graph.steps.length;
    list.querySelectorAll("[data-step]").forEach((link) => {
      if (Number(link.dataset.step) === shown) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
    controls.hidden = live || total === 0;
    if (!total) return;
    position.textContent = stepping
      ? `Step ${shown} of ${total}.`
      : live
        ? `${total} ${total === 1 ? "step has" : "steps have"} finished so far. ${
            graph.current && diagram ? "The highlighted node is running." : "The run is in progress."
          }`
        : `All ${total} steps of the run.`;
    load(shown, force);
  };

  const show = (count, replaying, force = false) => {
    shown = Math.max(1, Math.min(graph.steps.length, count));
    stepping = replaying;
    paint();
    describe(force);
  };

  const stop = () => {
    clearInterval(timer);
    timer = null;
    play.textContent = "Replay";
  };

  const plain = (event) =>
    event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;

  root.addEventListener("click", (event) => {
    if (!plain(event)) return;
    const tab = event.target.closest("[data-view]");
    if (tab && detail.contains(tab)) {
      event.preventDefault();
      view = tab.dataset.view;
      choose(view, query());
      return;
    }
    const row = event.target.closest("[data-step]");
    if (row && list.contains(row)) {
      event.preventDefault();
      stop();
      const number = Number(row.dataset.step);
      show(number, number < graph.steps.length);
    }
  });

  controls.addEventListener("click", (event) => {
    const action = event.target.closest("[data-replay]")?.dataset.replay;
    if (!action) return;
    const playing = timer !== null;
    stop();
    if (action === "first") show(1, true);
    if (action === "previous") show(stepping ? shown - 1 : graph.steps.length - 1, true);
    if (action === "next") show(stepping ? shown + 1 : 1, true);
    if (action === "last") show(graph.steps.length, false);
    if (action === "play" && !playing) {
      show(stepping && shown < graph.steps.length ? shown : 1, true);
      play.textContent = "Pause";
      timer = setInterval(() => {
        if (shown >= graph.steps.length) stop();
        else show(shown + 1, true);
      }, 1200);
    }
  });

  const loaded = performance.now();
  const elapsed = Number(clock?.dataset.elapsedMs);
  const tick =
    live && clock && Number.isFinite(elapsed) && elapsed >= 0
      ? setInterval(() => {
          const ms = elapsed + performance.now() - loaded;
          clock.textContent = ms < 1000 ? `${ms.toFixed(1)} ms` : `${(ms / 1000).toFixed(2)} s`;
        }, 1000)
      : null;

  const signature = (step) => JSON.stringify(Object.keys(step).sort().map((key) => [key, step[key]]));

  const follow = () => {
    const source = new EventSource(root.dataset.liveUrl);
    source.addEventListener("spans", (event) => {
      const update = JSON.parse(event.data).graph;
      if (!update) return;
      const previous = graph.steps.map(signature);
      Object.assign(graph, {
        steps: update.steps,
        finished: update.finished,
        status: update.status,
        ended_from: update.ended_from,
        current: update.current,
      });
      const grew = graph.steps.length !== previous.length;
      const changed = graph.steps
        .filter((step, index) => signature(step) !== previous[index] || (grew && index === previous.length - 1))
        .map((step) => step.number);
      if (grew) details.clear();
      else changed.forEach((number) => details.delete(number));
      list.replaceChildren(...graph.steps.map(item));
      const number = stepping ? shown : graph.steps.length;
      show(number, stepping, changed.includes(number));
      retitle();
    });
    source.addEventListener("end", () => {
      source.close();
      const address = new URL(root.dataset.pageUrl, window.location.href);
      if (stepping) address.searchParams.set("step", shown);
      address.searchParams.set("view", view);
      window.history.replaceState(null, "", address);
      window.location.reload();
    });
    source.addEventListener("idle", () => {
      source.close();
      clearInterval(tick);
      const note = document.getElementById("trace-live-note");
      if (note) note.textContent = "No new spans arrived for a while, so the page stopped following this run. Reload it to check again.";
    });
  };

  const draw = async () => {
    if (!diagram) return;
    if (!window.mermaid) throw new Error("Mermaid did not load.");
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
    await window.mermaid.run({ nodes: [diagram] });
    svg = diagram.querySelector("svg");
    paint();
  };

  describe();
  draw()
    .catch(() => {
      const note = document.getElementById("trace-diagram-note");
      if (note) note.hidden = false;
    })
    .finally(() => {
      if (live) follow();
    });
})();

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const SCRIPT = readFileSync(
  new URL("../../src/nodeartifact/server/static/trace.js", import.meta.url),
  "utf8",
);

class Element {
  constructor({ dataset = {}, text = "" } = {}) {
    this.dataset = dataset;
    this.textContent = text;
    this.hidden = false;
  }

  addEventListener() {}

  querySelector() {
    return null;
  }

  querySelectorAll() {
    return [];
  }

  contains() {
    return false;
  }
}

function loadRunningTrace({ elapsed, shown }) {
  const page = { clock: 0, intervals: [], sources: [] };
  const clock = new Element({ dataset: elapsed === undefined ? {} : { elapsedMs: String(elapsed) }, text: shown });
  const graph = { steps: [], finished: false, status: "running", ended_from: [], current: null };
  const elements = {
    "trace-graph": new Element({ dataset: { live: "true", liveUrl: "/traces/a/live?after=0", pageUrl: "/traces/a" } }),
    "trace-graph-data": new Element({ text: JSON.stringify(graph) }),
    "trace-step-list": new Element(),
    "trace-position": new Element(),
    "trace-replay": new Element(),
    "trace-step-detail": new Element(),
    "trace-duration": clock,
  };
  class EventSource {
    constructor(url) {
      this.url = url;
      page.sources.push(this);
    }

    addEventListener() {}
  }
  const context = {
    document: { getElementById: (id) => elements[id] ?? null },
    EventSource,
    performance: { now: () => page.clock },
    setInterval: (callback) => page.intervals.push(callback),
    clearInterval: () => {},
  };
  context.window = context;
  runInNewContext(SCRIPT, context);
  return {
    page,
    text: () => clock.textContent,
    tick(milliseconds) {
      page.clock += milliseconds;
      for (const callback of page.intervals) callback();
    },
  };
}

test("a running trace counts its duration up from the time the server measured", async () => {
  const trace = loadRunningTrace({ elapsed: 1500, shown: "1.50 s" });
  await new Promise((resolve) => setImmediate(resolve));

  trace.tick(1000);

  assert.equal(trace.text(), "2.50 s");
  assert.equal(trace.page.sources.length, 1);
});

test("a running trace from a clock that runs ahead of the server keeps its dash", async () => {
  for (const elapsed of [-119859, undefined]) {
    const trace = loadRunningTrace({ elapsed, shown: "-" });
    await new Promise((resolve) => setImmediate(resolve));

    trace.tick(2500);

    assert.equal(trace.text(), "-", String(elapsed));
    assert.equal(trace.page.intervals.length, 0, String(elapsed));
    assert.equal(trace.page.sources.length, 1, String(elapsed));
  }
});

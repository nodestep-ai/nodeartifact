import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyState, loadPage, row, table } from "./page.mjs";

const ROWS = ["a", "b", "c", "d"];
const PHONE_ROW = 300;
const TABLE_TOP = 200;

const top = (page, trace) => page.row(trace).getBoundingClientRect().top;

test("the page follows the stream from the row its results were rendered at", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });

  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=report&after=10");
});

test("a reader at the top of the page sees a new trace come in at the top", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });

  page.send(table(["new", ...ROWS].map((trace) => row(trace))), 11);

  assert.deepEqual(page.order(), ["new", ...ROWS]);
  assert.equal(page.scrollY, 0);
  assert.equal(top(page, "new"), TABLE_TOP);
});

test("a reader who has scrolled into the first row keeps it in place when a trace arrives", () => {
  const page = loadPage({
    results: table(ROWS.map((trace) => row(trace))),
    rowHeight: PHONE_ROW,
    scrollY: TABLE_TOP + 150,
  });
  const before = top(page, "a");

  page.send(table(["new", ...ROWS].map((trace) => row(trace))), 11);
  page.send(table(["newer", "new", ...ROWS].map((trace) => row(trace))), 12);

  assert.equal(before, -150);
  assert.equal(top(page, "a"), before);
  assert.equal(page.scrollY, TABLE_TOP + 150 + 2 * PHONE_ROW);
});

test("a reader who has scrolled a little keeps the first row in place", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))), scrollY: 30 });

  page.send(table(["new", ...ROWS].map((trace) => row(trace))), 11);

  assert.equal(top(page, "a"), TABLE_TOP - 30);
  assert.equal(page.scrollY, 30 + 56);
});

test("a reader past the first rows keeps the row they read in place", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))), scrollY: TABLE_TOP + 2 * 56 + 10 });
  const before = top(page, "c");

  page.send(table(["new", ...ROWS].map((trace) => row(trace))), 11);

  assert.equal(top(page, "c"), before);
});

test("the row the page keeps in place is the first one below the top bar", () => {
  const page = loadPage({
    results: table(ROWS.map((trace) => row(trace))),
    rowHeight: PHONE_ROW,
    scrollY: TABLE_TOP + PHONE_ROW - 40,
  });
  const before = top(page, "b");

  page.send(table(["b", "c", "d"].map((trace) => row(trace))), 11);

  assert.equal(page.row("a"), null);
  assert.equal(top(page, "b"), before);
});

test("rows that the update leaves out are removed and the rest follow its order", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });
  const kept = page.row("c");

  page.send(table(["d", "c", "a"].map((trace) => row(trace))), 11);

  assert.deepEqual(page.order(), ["d", "c", "a"]);
  assert.ok(page.row("c") === kept, "an unchanged row keeps its element");
});

test("a changed row is replaced, and the focus goes back to the link of the same trace", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });
  page.row("b").querySelector("a").focus();

  page.send(table(["new", "a", ...ROWS.slice(1)].map((trace) => row(trace, { duration: trace === "b" ? "2.00 s" : "1.00 s" }))), 11);

  const link = page.row("b").querySelector("a");
  assert.equal(link.textContent, "Trace b");
  assert.equal(page.row("b").querySelector('[data-label="Duration"]').textContent, "2.00 s");
  assert.ok(page.focused === link, "the focus is on the new link of trace b");
  assert.equal(page.focusOptions.at(-1)?.preventScroll, true);
});

test("the first trace turns the empty state into the table", () => {
  const page = loadPage({ results: '<div class="nodestep-empty"><h2>No traces yet</h2></div>' });

  page.send(table([row("first")]), 11);

  assert.deepEqual(page.order(), ["first"]);
  assert.equal(page.root.querySelector(".nodestep-empty"), null);
});

test("a running trace counts its duration up between updates", () => {
  const page = loadPage({ results: table([row("a", { elapsed: 1500, duration: "1.50 s" }), row("b")]) });

  page.tick(1000);
  const counted = page.row("a").querySelector('[data-label="Duration"]').textContent;
  page.tick(1000);

  assert.equal(counted, "2.50 s");
  assert.equal(page.row("a").querySelector('[data-label="Duration"]').textContent, "3.50 s");
  assert.equal(page.row("b").querySelector('[data-label="Duration"]').textContent, "1.00 s");
});

test("a negative elapsed time from a clock that runs ahead is not counted", () => {
  const page = loadPage({ results: table([row("a", { elapsed: -119859, duration: "-" })]) });

  page.tick(2500);

  assert.equal(page.row("a").querySelector('[data-label="Duration"]').textContent, "-");
});

test("the stream closes while the tab is hidden and opens again from the newest row when it is shown", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });
  const first = page.source();
  page.send(table(["new", ...ROWS].map((trace) => row(trace))), 42);

  page.hide();
  const hidden = page.open().length;
  const clocks = page.intervals.size;
  page.show();

  assert.ok(first.closed);
  assert.equal(hidden, 0);
  assert.equal(clocks, 0);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=report&after=42");
  assert.equal(page.intervals.size, 1);
});

test("a page that loads in a hidden tab waits until it is shown", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))), hidden: true });
  const waiting = page.sources.length;

  page.show();

  assert.equal(waiting, 0);
  assert.equal(page.open().length, 1);
});

test("leaving the page closes the stream", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });

  page.fire("window", "pagehide", { persisted: true });

  assert.equal(page.open().length, 0);
});

test("a page brought back from the back-forward cache shows the query of its results again", () => {
  const page = loadPage({ results: table(ROWS.map((trace) => row(trace))) });
  page.field().value = "typed before leaving";

  page.fire("window", "pagehide", { persisted: true });
  page.fire("window", "pageshow", { persisted: true });

  assert.equal(page.field().value, "report");
  assert.equal(page.open().length, 1);
});

const PAUSE = 250;
const LIST = "http://127.0.0.1:8000/";
const listed = (traces) => table(traces.map((trace) => row(trace)));

test("text typed before the script runs is searched after the usual pause", async () => {
  const page = loadPage({ results: listed(ROWS), typed: "refund" });
  const early = page.fetches.length;

  page.wait(PAUSE);
  page.fetches[0].respond(listed(["r"]));
  await page.settle();
  page.fire("window", "pageshow", { persisted: false });

  assert.equal(early, 0);
  assert.equal(page.field().value, "refund");
  assert.deepEqual(
    page.fetches.map((request) => request.url),
    ["http://127.0.0.1:8000/results?q=refund"],
  );
  assert.deepEqual(page.order(), ["r"]);
  assert.deepEqual(page.replaced, [`${LIST}?q=refund`]);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=refund&after=10");
});

test("a page that loads with its own query, or the same words spaced otherwise, searches nothing", () => {
  for (const typed of [undefined, "  report "]) {
    const page = loadPage({ results: listed(ROWS), typed });

    page.wait(PAUSE);

    assert.equal(page.fetches.length, 0, String(typed));
    assert.equal(page.open().length, 1, String(typed));
  }
});

test("typing filters the list after a short pause and the address follows the query", async () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("refund");
  page.wait(PAUSE - 1);
  const early = page.fetches.length;
  page.wait(1);
  page.fetches[0].respond(listed(["r"]));
  await page.settle();

  assert.equal(early, 0);
  assert.deepEqual(
    page.fetches.map((request) => request.url),
    ["http://127.0.0.1:8000/results?q=refund"],
  );
  assert.deepEqual(page.order(), ["r"]);
  assert.deepEqual(page.replaced, [`${LIST}?q=refund`]);
  assert.equal(page.assigned.length, 0);
});

test("each key press starts the pause again, so a word typed quickly is searched once", async () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("r");
  page.wait(100);
  page.type("re");
  page.wait(100);
  page.type("ref");
  page.wait(PAUSE);

  assert.deepEqual(
    page.fetches.map((request) => request.url),
    ["http://127.0.0.1:8000/results?q=ref"],
  );
});

test("the same query with other spacing is not searched again", () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("  report ");
  page.wait(PAUSE);

  assert.equal(page.fetches.length, 0);
  assert.equal(page.open().length, 1);
});

test("while new results load the stream is closed, then it follows the new query from where it was", async () => {
  const page = loadPage({ results: listed(ROWS) });
  const first = page.source();
  page.send(listed(["new", ...ROWS]), 42);

  page.type("refund");
  page.wait(PAUSE);
  const closed = page.open().length;
  page.fetches[0].respond(listed(["r"]));
  await page.settle();
  page.send(listed(["r2", "r"]), 43);

  assert.ok(first.closed);
  assert.equal(closed, 0);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=refund&after=42");
  assert.deepEqual(page.order(), ["r2", "r"]);
});

test("an answer for an older query is dropped", async () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("a");
  page.wait(PAUSE);
  page.type("ab");
  page.wait(PAUSE);
  page.fetches[0].respond(listed(["old"]));
  await page.settle();
  page.fetches[1].respond(listed(["new"]));
  await page.settle();

  assert.ok(page.fetches[0].signal.aborted);
  assert.deepEqual(page.order(), ["new"]);
  assert.deepEqual(page.replaced, [`${LIST}?q=a`, `${LIST}?q=ab`]);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=ab&after=10");
});

test("Escape clears the field and shows every trace at once, and on an empty field leaves it", async () => {
  const page = loadPage({ results: listed(["r"]) });
  page.field().focus();

  const cleared = page.press("Escape");
  page.fetches[0].respond(listed(ROWS));
  await page.settle();
  const focusAfterClear = page.focused;
  const left = page.press("Escape");

  assert.ok(cleared.defaultPrevented);
  assert.equal(page.field().value, "");
  assert.deepEqual(
    page.fetches.map((request) => request.url),
    ["http://127.0.0.1:8000/results?q="],
  );
  assert.deepEqual(page.order(), ROWS);
  assert.deepEqual(page.replaced, [LIST]);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=&after=10");
  assert.ok(focusAfterClear === page.field(), "the field keeps the focus after the first Escape");
  assert.ok(left.defaultPrevented);
  assert.equal(page.focused, null);
  assert.equal(page.fetches.length, 1);
});

test("Escape while an input method is composing a word only ends the composition", () => {
  const page = loadPage({ results: listed(ROWS) });
  page.field().focus();
  page.field().value = "report とうきょう";

  const pressed = page.press("Escape", { isComposing: true });
  page.wait(PAUSE);

  assert.ok(pressed.defaultPrevented, "the browser does not empty the search field itself");
  assert.equal(page.field().value, "report とうきょう");
  assert.equal(page.fetches.length, 0);
  assert.deepEqual(page.replaced, []);
  assert.ok(page.focused === page.field(), "the field keeps the focus");
});

test("other keys type as usual", () => {
  const page = loadPage({ results: listed(ROWS) });

  assert.ok(!page.press("Enter").defaultPrevented);
  assert.ok(!page.press("a").defaultPrevented);
  assert.equal(page.fetches.length, 0);
});

test("Enter searches at once without leaving the page", () => {
  const page = loadPage({ results: listed(ROWS) });
  page.field().value = "refund";

  const submitted = page.submit();

  assert.ok(submitted.defaultPrevented);
  assert.deepEqual(
    page.fetches.map((request) => request.url),
    ["http://127.0.0.1:8000/results?q=refund"],
  );
});

test("the clear button in the field clears the search in place and keeps the focus in the field", async () => {
  const page = loadPage({ results: listed(["r"]) });

  const click = page.click(page.clear().querySelector("path"));
  page.fetches[0].respond(listed(ROWS));
  await page.settle();

  assert.ok(click.defaultPrevented);
  assert.equal(page.field().value, "");
  assert.deepEqual(page.order(), ROWS);
  assert.deepEqual(page.replaced, [LIST]);
  assert.ok(page.focused === page.field(), "the field has the focus");
});

test("the clear button of the empty state clears the search in place and moves the focus to the content", async () => {
  const page = loadPage({ results: emptyState() });
  const clear = page.root.querySelector("#trace-results").querySelector("[data-clear-search]");
  clear.focus();

  const click = page.click(clear);
  page.fetches[0].respond(listed(ROWS));
  await page.settle();

  assert.ok(click.defaultPrevented);
  assert.equal(page.field().value, "");
  assert.deepEqual(page.order(), ROWS);
  assert.deepEqual(page.replaced, [LIST]);
  assert.ok(page.focused === page.root.querySelector("#content"), "the focus is on the main content");
  assert.equal(page.focusOptions.at(-1)?.preventScroll, true);
});

test("other clicks are left alone", () => {
  const page = loadPage({ results: listed(ROWS) });

  const click = page.click(page.row("a").querySelector("a"));

  assert.ok(!click.defaultPrevented);
  assert.equal(page.fetches.length, 0);
});

test("a failed search loads the page for the query instead", async () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("refund");
  page.wait(PAUSE);
  page.fetches[0].respond("Internal Server Error", { ok: false });
  await page.settle();

  assert.deepEqual(page.assigned, [`${LIST}?q=refund`]);
  assert.deepEqual(page.order(), ROWS);
});

test("a search in a hidden tab waits to follow the stream until the tab is shown", async () => {
  const page = loadPage({ results: listed(ROWS) });

  page.type("refund");
  page.wait(PAUSE);
  page.hide();
  page.fetches[0].respond(listed(["r"]));
  await page.settle();
  const hidden = page.open().length;
  page.show();

  assert.equal(hidden, 0);
  assert.deepEqual(page.order(), ["r"]);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=refund&after=10");
});

test("a page brought back from the back-forward cache shows the query it last searched", async () => {
  const page = loadPage({ results: listed(ROWS) });
  page.type("refund");
  page.wait(PAUSE);
  page.fetches[0].respond(listed(["r"]));
  await page.settle();
  page.type("refund and more");

  page.fire("window", "pagehide", { persisted: true });
  page.fire("window", "pageshow", { persisted: true });
  page.wait(PAUSE);

  assert.equal(page.field().value, "refund");
  assert.equal(page.fetches.length, 1);
  assert.equal(page.open().length, 1);
  assert.equal(page.source().url, "http://127.0.0.1:8000/live?q=refund&after=10");
});

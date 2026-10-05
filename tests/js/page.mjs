import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const SCRIPT = readFileSync(
  new URL("../../src/nodeartifact/server/static/traces.js", import.meta.url),
  "utf8",
);
const VOID_TAGS = new Set(["br", "hr", "img", "input", "link", "meta"]);
const ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', "#34": '"', "#39": "'" };
const TOP_BAR_HEIGHT = 56;

const decode = (text) => text.replace(/&(#?\w+);/g, (entity, name) => ENTITIES[name] ?? entity);

class Text {
  constructor(data) {
    this.data = data;
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }

  isEqualNode(other) {
    return other instanceof Text && other.data === this.data;
  }
}

class Element {
  #page;
  #listeners = [];

  constructor(page, tag) {
    this.#page = page;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.value = "";
  }

  addEventListener(type, listener) {
    this.#listeners.push([type, listener]);
  }

  dispatch(type, init = {}) {
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      ...init,
    };
    for (const [name, listener] of this.#listeners) if (name === type) listener(event);
    return event;
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  get dataset() {
    const attribute = (key) => `data-${key.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
    return new Proxy({}, { get: (target, key) => this.getAttribute(attribute(String(key))) ?? undefined });
  }

  get classList() {
    return { contains: (name) => (this.getAttribute("class") ?? "").split(/\s+/).includes(name) };
  }

  get defaultValue() {
    return this.getAttribute("value") ?? "";
  }

  get children() {
    return this.childNodes.filter((node) => node instanceof Element);
  }

  get lastElementChild() {
    return this.children.at(-1) ?? null;
  }

  get parentElement() {
    return this.parentNode;
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.#page.root;
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  set textContent(text) {
    this.replaceChildren(new Text(String(text)));
  }

  append(...nodes) {
    for (const node of nodes) this.insertBefore(node, null);
  }

  insertBefore(node, reference) {
    node.parentNode?.childNodes.splice(node.parentNode.childNodes.indexOf(node), 1);
    const index = reference ? this.childNodes.indexOf(reference) : this.childNodes.length;
    this.childNodes.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }

  replaceWith(node) {
    const parent = this.parentNode;
    parent.insertBefore(node, this);
    this.remove();
  }

  remove() {
    this.parentNode?.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }

  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }

  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }

  isEqualNode(other) {
    return (
      other instanceof Element &&
      other.tagName === this.tagName &&
      JSON.stringify([...other.attributes]) === JSON.stringify([...this.attributes]) &&
      other.childNodes.length === this.childNodes.length &&
      this.childNodes.every((node, index) => node.isEqualNode(other.childNodes[index]))
    );
  }

  matches(selector) {
    return selector.split(",").some((part) => {
      const pattern = /^([a-z]+)?((?:[.#][\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/;
      const [, tag, rest] = pattern.exec(part.trim()) ?? [];
      if (rest === undefined) throw new Error(`the fake page cannot read ${selector}`);
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      return [...rest.matchAll(/([.#])([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g)].every(
        ([, sign, name, attribute, value]) => {
          if (sign === ".") return this.classList.contains(name);
          if (sign === "#") return this.getAttribute("id") === name;
          return value === undefined ? this.attributes.has(attribute) : this.getAttribute(attribute) === value;
        },
      );
    });
  }

  closest(selector) {
    for (let node = this; node instanceof Element; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }

  querySelectorAll(selector) {
    const found = [];
    const visit = (element) => {
      for (const child of element.children) {
        if (child.matches(selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  focus(options) {
    this.#page.focused = this;
    this.#page.focusOptions.push(options);
  }

  blur() {
    if (this.#page.focused === this) this.#page.focused = null;
  }

  getBoundingClientRect() {
    return this.#page.box(this);
  }
}

function parse(page, markup) {
  const fragment = new Element(page, "body");
  const open = [fragment];
  const pattern = /<\/([a-z]+)>|<([a-z]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>/g;
  let position = 0;
  for (const match of markup.matchAll(pattern)) {
    if (match.index > position) open.at(-1).append(new Text(decode(markup.slice(position, match.index))));
    position = match.index + match[0].length;
    const [, closing, tag, attributes] = match;
    if (closing) {
      open.pop();
      continue;
    }
    const element = new Element(page, tag);
    for (const [, name, value] of attributes.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
      element.setAttribute(name, decode(value ?? ""));
    }
    if (tag === "input") element.value = element.defaultValue;
    open.at(-1).append(element);
    if (!VOID_TAGS.has(tag)) open.push(element);
  }
  if (position < markup.length) open.at(-1).append(new Text(decode(markup.slice(position))));
  return fragment;
}

export function row(trace, { title = `Trace ${trace}`, duration = "1.00 s", elapsed } = {}) {
  const clock = elapsed === undefined ? "" : ` data-elapsed-ms="${elapsed}"`;
  return (
    `<tr data-trace="${trace}"><td class="name" data-label="Trace"><span><a href="/traces/${trace}">${title}</a></span></td>` +
    `<td class="nodestep-num" data-label="Duration"${clock}>${duration}</td></tr>`
  );
}

export function table(rows) {
  return (
    '<div class="nodestep-table-wrap"><table class="nodestep-table traces"><thead><tr><th>Trace</th><th>Duration</th></tr></thead>' +
    `<tbody>${rows.join("")}</tbody></table></div>`
  );
}

export function emptyState() {
  return (
    '<div class="nodestep-empty"><h2 class="nodestep-empty-title">No traces match this search</h2>' +
    '<p><a class="nodestep-button nodestep-button-secondary" href="http://127.0.0.1:8000/" data-clear-search>Clear the search</a></p></div>'
  );
}

export function loadPage({ results, query = "report", typed, rowHeight = 56, tableTop = 200, scrollY = 0, hidden = false }) {
  const page = {
    root: null,
    focused: null,
    focusOptions: [],
    scrollY,
    hidden,
    clock: 0,
    sources: [],
    intervals: new Map(),
    timeouts: new Map(),
    fetches: [],
    replaced: [],
    assigned: [],
    href: `http://127.0.0.1:8000/?q=${query}`,
    listeners: { document: [], window: [] },
    box(element) {
      if (element.matches(".nodestep-topbar")) return { top: 0, bottom: TOP_BAR_HEIGHT, height: TOP_BAR_HEIGHT };
      const rows = page.rows();
      const index = rows.indexOf(element);
      if (index < 0) return { top: 0, bottom: 0, height: 0 };
      const top = tableTop + index * rowHeight - page.scrollY;
      return { top, bottom: top + rowHeight, height: rowHeight };
    },
    rows: () => page.root.querySelector("#trace-results").querySelectorAll("tr[data-trace]"),
    row: (trace) => page.root.querySelector(`tr[data-trace="${trace}"]`),
    order: () => page.rows().map((element) => element.dataset.trace),
    field: () => page.root.querySelector("#trace-search"),
    form: () => page.root.querySelector("form"),
    clear: () => page.root.querySelector(".nodestep-search-clear"),
    type(text) {
      page.field().value = text;
      page.field().dispatch("input");
    },
    press(key, init = {}) {
      return page.field().dispatch("keydown", { key, ...init });
    },
    submit() {
      return page.form().dispatch("submit");
    },
    click(element) {
      const event = {
        type: "click",
        target: element,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
      };
      for (const [name, listener] of page.listeners.document) if (name === "click") listener(event);
      return event;
    },
    wait(milliseconds) {
      page.clock += milliseconds;
      for (const [id, [due, callback]] of [...page.timeouts]) {
        if (due <= page.clock) {
          page.timeouts.delete(id);
          callback();
        }
      }
    },
    settle: () => new Promise((resolve) => setImmediate(resolve)),
    source: () => page.sources.at(-1),
    open: () => page.sources.filter((source) => !source.closed),
    send(html, id) {
      page.source().emit("traces", { html }, String(id));
    },
    tick(milliseconds) {
      page.clock += milliseconds;
      for (const callback of page.intervals.values()) callback();
    },
    fire(target, type, event = {}) {
      for (const [name, listener] of page.listeners[target]) if (name === type) listener({ type, ...event });
    },
    hide() {
      page.hidden = true;
      page.fire("document", "visibilitychange");
    },
    show() {
      page.hidden = false;
      page.fire("document", "visibilitychange");
    },
  };
  page.root = new Element(page, "html");
  page.root.append(
    parse(
      page,
      '<header class="nodestep-topbar"><div class="nodestep-topbar-end">' +
        '<form class="nodestep-search" role="search" action="http://127.0.0.1:8000/" method="get">' +
        `<input class="nodestep-search-input" id="trace-search" type="search" name="q" value="${query}">` +
        '<a class="nodestep-search-clear" href="http://127.0.0.1:8000/" data-clear-search><svg><path></path></svg></a>' +
        "</form></div></header>" +
        `<main id="content" tabindex="-1"><div class="trace-results" id="trace-results" data-live-url="http://127.0.0.1:8000/live?q=${query}&amp;after=10" data-results-url="http://127.0.0.1:8000/results">${results}</div></main>`,
    ),
  );

  class EventSource {
    constructor(url) {
      this.url = String(url);
      this.closed = false;
      this.handlers = [];
      page.sources.push(this);
    }

    addEventListener(type, handler) {
      this.handlers.push([type, handler]);
    }

    close() {
      this.closed = true;
    }

    emit(type, data, lastEventId) {
      for (const [name, handler] of this.handlers) if (name === type) handler({ data: JSON.stringify(data), lastEventId });
    }
  }

  const document = {
    getElementById: (id) => page.root.querySelector(`#${id}`),
    querySelector: (selector) => page.root.querySelector(selector),
    get activeElement() {
      return page.focused?.isConnected ? page.focused : page.root.querySelector("body");
    },
    get hidden() {
      return page.hidden;
    },
    addEventListener: (type, listener) => page.listeners.document.push([type, listener]),
  };
  let timers = 0;
  const fetch = (url, { signal } = {}) =>
    new Promise((resolve, reject) => {
      const request = {
        url: String(url),
        signal,
        respond(html, { ok = true } = {}) {
          if (signal?.aborted) return;
          resolve({ ok, status: ok ? 200 : 500, text: async () => html });
        },
      };
      signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      page.fetches.push(request);
    });
  const context = {
    document,
    EventSource,
    URL,
    AbortController,
    fetch,
    setTimeout: (callback, milliseconds) => {
      timers += 1;
      page.timeouts.set(timers, [page.clock + milliseconds, callback]);
      return timers;
    },
    clearTimeout: (timer) => page.timeouts.delete(timer),
    history: {
      state: null,
      replaceState(state, title, url) {
        page.replaced.push(String(url));
        page.href = String(url);
      },
    },
    DOMParser: class {
      parseFromString(markup) {
        return { body: parse(page, markup) };
      }
    },
    performance: { now: () => page.clock },
    setInterval: (callback) => {
      timers += 1;
      page.intervals.set(timers, callback);
      return timers;
    },
    clearInterval: (timer) => page.intervals.delete(timer),
    location: {
      get href() {
        return page.href;
      },
      assign(url) {
        page.assigned.push(String(url));
      },
    },
    get scrollY() {
      return page.scrollY;
    },
    scrollBy: (x, y) => {
      page.scrollY = Math.max(0, page.scrollY + y);
    },
    addEventListener: (type, listener) => page.listeners.window.push([type, listener]),
  };
  context.window = context;
  if (typed !== undefined) page.field().value = typed;
  runInNewContext(SCRIPT, context);
  return page;
}

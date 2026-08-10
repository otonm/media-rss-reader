// ---------------------------------------------------------------------------
// dom-mock — minimal browser-API shim for testing the static JS modules.
//
// The src/static/*.js files are scripts, not ESM. They use `window.MRR`,
// `document.createElement`, `setTimeout`, `clearTimeout`, etc. We provide
// just enough of the browser surface to load and exercise them under
// `node --test`.
//
// Public API:
//   createDomContext()       — context object suitable for vm.createContext()
//   installGlobals(ctx)      — set globals (window, document, ...) on the
//                              current Node global so loaded scripts that
//                              read from `window` work without vm.
//   loadScript(absPath, ctx) — read a src/static/*.js file and execute it
//                              inside the given context.
// ---------------------------------------------------------------------------

import vm from "node:vm";
import { readFileSync } from "node:fs";

export function createDomContext() {
  // The default setInterval is replaced with a no-op + tracker so test
  // code can either drain or disable the recurring buffer-threshold
  // evaluations that production's playWhenBufferedAndVisible sets up.
  // Tests that DO want the real interval should call
  // `setRealTimers(ctx)` after loading the module.
  const intervals = new Set();
  let nextId = 1;
  const ctx = {
    // Browser global. `MRR` is the namespace all modules attach to. It is an
    // event target because scroll-controller listens for `pagehide`; call
    // `window.dispatchEvent({ type: "pagehide" })` to fire it.
    window: Object.assign(makeEventTarget(), { MRR: {} }),
    document: makeDocument(),
    // Default no-op beacon; harnesses that assert on seen-marking replace
    // this with a recorder, the way they replace `fetch`.
    navigator: { sendBeacon: () => true },
    setTimeout,
    clearTimeout,
    setInterval: (...args) => {
      const id = nextId++;
      intervals.add(id);
      return id;
    },
    clearInterval: (id) => {
      intervals.delete(id);
    },
    _intervals: intervals,
    fetch: () => Promise.resolve({ ok: false }),
    console,
  };
  // ponytail: minimal Image shim so tests can exercise gallery slides.
  // feed-view.js does `new Image()` for non-first slides; the real browser
  // supplies this. Tests need a no-op stand-in to reach the broken line.
  class _Image {
    constructor() {
      Object.assign(this, {
        tagName: "IMG", nodeName: "IMG", src: "", className: "", dataset: {},
        children: [], parentNode: null, style: {}, attributes: {},
        naturalWidth: 320, naturalHeight: 240,
        _listeners: new Map(),
        setAttribute() {},
        getAttribute() { return null; },
        addEventListener(name, fn, opts) {
          const arr = this._listeners.get(name) || [];
          arr.push({ fn, once: !!(opts && opts.once) });
          this._listeners.set(name, arr);
        },
        removeEventListener(name, fn) {
          const arr = this._listeners.get(name);
          if (!arr) return;
          const idx = arr.findIndex((e) => e.fn === fn);
          if (idx >= 0) arr.splice(idx, 1);
        },
        dispatchEvent(evt) {
          const arr = this._listeners.get(evt.type) || [];
          for (const e of arr.slice()) {
            e.fn(evt);
            if (e.once) arr.splice(arr.indexOf(e), 1);
          }
        },
        cloneNode() { return new _Image(); },
      });
      ctx._images.push(this);
    }
  }
  ctx.Image = _Image;
  // Every Image the code under test constructs, in construction order. Tests
  // use it to fire load/error on a specific download.
  ctx._images = [];
  vm.createContext(ctx);
  return ctx;
}

// Replace setTimeout/clearTimeout with a manually-advanced clock. Needed by
// anything that arms a deadline: cache-queue's per-download timeout would
// otherwise hold a real 10s timer open and stall the whole test run.
export function fakeTimeout(ctx) {
  const timers = new Map();
  let nextId = 1;
  ctx.setTimeout = (fn, ms) => {
    const id = nextId++;
    timers.set(id, { fn, ms });
    return id;
  };
  ctx.clearTimeout = (id) => timers.delete(id);
  return {
    /** Fire every timer whose delay is <= ms. */
    advance(ms) {
      for (const [id, timer] of [...timers]) {
        if (timer.ms <= ms) {
          timers.delete(id);
          timer.fn();
        }
      }
    },
    pending: () => timers.size,
  };
}

// Replace the mock setInterval/clearInterval with the real ones. Use this
// in tests that actually need the buffer-threshold loop to fire.
export function setRealTimers(ctx) {
  ctx.setInterval = setInterval;
  ctx.clearInterval = clearInterval;
  delete ctx._intervals;
}

export function installGlobals(ctx) {
  for (const k of Object.keys(ctx)) globalThis[k] = ctx[k];
}

export function loadScript(absPath, ctx) {
  const src = readFileSync(absPath, "utf8");
  vm.runInContext(src, ctx, { filename: absPath });
}

// Minimal EventTarget: same listener-map shape the elements use.
function makeEventTarget() {
  return {
    _listeners: new Map(),
    addEventListener(name, fn) {
      const arr = this._listeners.get(name) || [];
      arr.push(fn);
      this._listeners.set(name, arr);
    },
    removeEventListener(name, fn) {
      const arr = this._listeners.get(name);
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatchEvent(evt) {
      for (const fn of (this._listeners.get(evt.type) || []).slice()) fn(evt);
    },
  };
}

// ---------------------------------------------------------------------------
// Document — supports createElement and getElementById, with a tiny registry
// so getElementById can return a registered node.
// ---------------------------------------------------------------------------
function makeDocument() {
  const byId = new Map();

  function createElement(tag) {
    return makeElement(tag);
  }

  function getElementById(id) {
    return byId.get(id) || null;
  }

  function register(el) {
    if (el && el.id) byId.set(el.id, el);
  }

  // Document-level querySelector delegates to the registered elements
  // (we only ever have one root in the test, the #feed container).
  function querySelector(selector) {
    for (const root of byId.values()) {
      const found = root.querySelector?.(selector);
      if (found) return found;
    }
    return null;
  }

  function querySelectorAll(selector) {
    const out = [];
    for (const root of byId.values()) {
      out.push(...(root.querySelectorAll?.(selector) || []));
    }
    return out;
  }

  // controls.js appends the UI_DEBUG overlay straight to document.body.
  const body = makeElement("body");

  return { createElement, getElementById, register, querySelector, querySelectorAll, body };
}

// ---------------------------------------------------------------------------
// Element — minimal subset. `class` is exposed as className (read+write),
// `dataset` is a plain object, `children` is an array, `addEventListener` /
// `removeEventListener` track listeners in a map keyed by event name (each
// value is an array of {fn, once}). appendChild / replaceWith mutate the
// parent and the element's parent pointer.
// ---------------------------------------------------------------------------
function makeElement(tag) {
  const el = {
    tagName: tag.toUpperCase(),
    nodeName: tag.toUpperCase(),
    id: "",
    className: "",
    dataset: {},
    // Custom properties (--t on the gallery dots) only go through setProperty;
    // plain assignment does nothing in a real browser.
    style: {
      setProperty(name, value) { this[name] = String(value); },
      getPropertyValue(name) { return Object.hasOwn(this, name) ? this[name] : ""; },
    },
    children: [],
    parentNode: null,
    attributes: {},
    textContent: "",
    innerHTML: "",
    src: "",
    href: "",
    muted: false,
    loop: false,
    preload: "",
    currentTime: 0,
    duration: 0,
    buffered: { length: 0, start: () => 0, end: () => 0 },
    paused: true,
    naturalWidth: 320,
    naturalHeight: 240,
    _listeners: new Map(),

    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name === "id") this.id = String(value);
      if (name === "class") this.className = String(value);
      if (name === "src") this.src = String(value);
    },
    getAttribute(name) {
      return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    replaceChildren(...nodes) {
      this.children.forEach((c) => { c.parentNode = null; });
      nodes.forEach((n) => { n.parentNode = this; });
      this.children = nodes;
    },
    replaceWith(...nodes) {
      if (this.parentNode) {
        const idx = this.parentNode.children.indexOf(this);
        if (idx >= 0) this.parentNode.children.splice(idx, 1, ...nodes);
        for (const n of nodes) n.parentNode = this.parentNode;
        this.parentNode = null;
      }
      return this;
    },
    remove() {
      if (this.parentNode) {
        const idx = this.parentNode.children.indexOf(this);
        if (idx >= 0) this.parentNode.children.splice(idx, 1);
        this.parentNode = null;
      }
    },
    // Records the call so a test can assert which element navigation landed on.
    scrollIntoView() {
      this.scrolledIntoView = (this.scrolledIntoView || 0) + 1;
    },
    // Real DOM properties, modelled because feed-view navigates by sibling
    // rather than by index — the store and the DOM are different index spaces.
    get nextElementSibling() {
      return siblingOf(this, 1);
    },
    get previousElementSibling() {
      return siblingOf(this, -1);
    },
    querySelector(selector) {
      const s = stripScope(selector);
      // Support the small subset used in the production code:
      //   ":scope > tag"           — direct child by tag (no :scope support in mock)
      //   ".class"                 — first child with class
      //   "tag"                    — first child matching tag
      //   ".class[data-id='...']"  — class + exact data-id value
      for (const c of this.children) {
        if (matchesSelector(c, s)) return c;
        const found = c.querySelector?.(s);
        if (found) return found;
      }
      return null;
    },
    querySelectorAll(selector) {
      const s = stripScope(selector);
      const out = [];
      const visit = (el) => {
        for (const c of el.children) {
          if (matchesSelector(c, s)) out.push(c);
          visit(c);
        }
      };
      visit(this);
      return out;
    },
    addEventListener(name, fn, opts) {
      const arr = this._listeners.get(name) || [];
      arr.push({ fn, once: !!(opts && opts.once) });
      this._listeners.set(name, arr);
    },
    removeEventListener(name, fn) {
      const arr = this._listeners.get(name);
      if (!arr) return;
      const idx = arr.findIndex((e) => e.fn === fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatchEvent(evt) {
      const arr = this._listeners.get(evt.type) || [];
      for (const e of arr.slice()) {
        e.fn(evt);
        if (e.once) arr.splice(arr.indexOf(e), 1);
      }
    },
    pause() { this.paused = true; },
    play() { this.paused = false; return Promise.resolve(); },
    getContext(kind) {
      if (kind !== "2d") return null;
      // Spy-friendly 2D context: record drawImage calls so tests can assert.
      this._ctxCalls = this._ctxCalls || [];
      return {
        drawImage: (...args) => this._ctxCalls.push({ method: "drawImage", args }),
        fillRect: () => {},
        clearRect: () => {},
      };
    },
    closest(selector) {
      for (let el = this; el; el = el.parentNode) {
        if (matchesSelector(el, selector)) return el;
      }
      return null;
    },
    cloneNode() { return makeElement(this.tagName.toLowerCase()); },
  };
  // classList over the className string — the production code uses the real
  // DOM API (feed-view's gallery slides, zoom-controller's .zoomed marker).
  el.classList = {
    contains: (c) => el.className.split(/\s+/).includes(c),
    add(c) { if (!this.contains(c)) el.className = (el.className + " " + c).trim(); },
    remove(c) {
      el.className = el.className.split(/\s+/).filter((x) => x && x !== c).join(" ");
    },
    toggle(c, on) { if (on === undefined ? this.contains(c) : !on) this.remove(c); else this.add(c); },
  };
  return el;
}

function matches(el, selector) {
  switch (selector) {
    case "video": return el.tagName === "VIDEO";
    case "img": return el.tagName === "IMG";
    case "canvas": return el.tagName === "CANVAS";
    default: return false;
  }
}

// The element `step` positions away from `el` among its parent's children.
function siblingOf(el, step) {
  if (!el.parentNode) return null;
  const idx = el.parentNode.children.indexOf(el);
  if (idx < 0) return null;
  return el.parentNode.children[idx + step] || null;
}

// Strip :scope pseudo-class from a compound selector so ":scope > video" becomes "> video".
function stripScope(s) { return s.replace(/:scope\s*/g, "").replace(/>\s*/g, ""); }

// Supports the small subset of CSS selectors used by the production code.
function matchesSelector(el, selector) {
  // Comma-separated: try each part
  if (selector.includes(",")) {
    return selector.split(",").some((part) => matchesSelector(el, part.trim()));
  }
  // Compound with id: "#id .class[data-attr='value']"
  let m = selector.match(/^#([\w-]+)\s+\.([\w-]+)\[data-([\w-]+)=['"]([\w-]+)['"]\]$/);
  if (m) return el.id === m[1] && el.className === m[2] && el.dataset?.[m[3]] === m[4];
  // Compound: ".class[data-id='value']"
  m = selector.match(/^\.([\w-]+)\[data-([\w-]+)=['"]([\w-]+)['"]\]$/);
  if (m) return el.className === m[1] && el.dataset?.[m[2]] === m[3];
  // Class only: ".class"
  m = selector.match(/^\.([\w-]+)$/);
  if (m) return el.className === m[1];
  // ID only: "#id"
  m = selector.match(/^#([\w-]+)$/);
  if (m) return el.id === m[1];
  // Tag only: "tag"
  return matches(el, selector);
}

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
    // Browser global. `MRR` is the namespace all modules attach to.
    window: { MRR: {} },
    document: makeDocument(),
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
    }
  }
  ctx.Image = _Image;
  vm.createContext(ctx);
  return ctx;
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

  return { createElement, getElementById, register, querySelector, querySelectorAll };
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
    style: {},
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

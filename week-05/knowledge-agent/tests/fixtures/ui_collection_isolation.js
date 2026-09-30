"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.style = {};
    this.text = "";
    this.classList = {
      add: (name) => { this.className = [...new Set([...this.className.split(" "), name])].join(" "); },
      remove: (name) => { this.className = this.className.split(" ").filter((item) => item !== name).join(" "); },
      toggle: (name, flag) => flag ? this.classList.add(name) : this.classList.remove(name),
    };
  }
  appendChild(child) { this.children.push(child); return child; }
  get lastElementChild() { return this.children.at(-1); }
  get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
  set innerHTML(value) { assert.equal(value, ""); this.text = ""; this.children = []; }
  querySelector(tag) { return this.children.find((child) => child.tagName === tag); }
}

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
element("versions").appendChild(new Element("tbody"));
element("search").textContent = "Search";
element("query").value = "memory";
element("top-k").value = "5";
element("search-strategy").value = "active";
element("chunks-active").checked = true;
const collections = [
  { collection_id: "a", name: "A", active_index_version_id: "version-a" },
  { collection_id: "b", name: "B", active_index_version_id: "version-b" },
];
const version = (id, collection) => ({
  index_version_id: id, collection_id: collection, strategy: "fixed", status: "ready",
  counts: { sources: 1, documents: 1, sections: 2, chunks: 1 },
});
const response = (payload) => ({ ok: true, json: async () => payload });
const queues = { versions: [], search: [], compare: [] };
const chunkRequests = [];
let delay = false;
let health = { embedding: { reachable: true, model_present: true, model: "fake" } };
const deferred = (queue, key) => new Promise((resolve) => queue.push({ key, resolve }));
const fetch = async (url, options = {}) => {
  if (url === "/api/health") return response(health);
  if (url === "/api/collections" && options.method === "POST") {
    const created = { collection_id: "c", name: "Created", active_index_version_id: null };
    collections.push(created);
    return response(created);
  }
  if (url === "/api/collections") return response({ collections: [...collections] });
  const list = url.match(/^\/api\/collections\/([^/]+)\/index-versions$/);
  if (list) {
    if (delay) return deferred(queues.versions, list[1]);
    return response({ index_versions: [version("version-" + list[1], list[1])] });
  }
  if (url === "/api/search") return deferred(queues.search, JSON.parse(options.body).collection_id);
  if (url.startsWith("/api/compare")) return deferred(queues.compare, new URL(url, "http://localhost").searchParams.get("collection_id"));
  const chunks = url.match(/^\/api\/index-versions\/([^/]+)\/chunks/);
  if (chunks) {
    chunkRequests.push(chunks[1]);
    return response({ items: [{ metadata: { section_path: "Body" }, text: chunks[1], token_count: 1, char_count: 1 }], total: 1 });
  }
  throw new Error("unexpected mocked request: " + url);
};
const context = vm.createContext({
  document: { getElementById: element, createElement: (tag) => new Element(tag), createTextNode: (text) => {
    const node = new Element("#text"); node.textContent = text; return node;
  } },
  fetch, setInterval: () => 1, clearInterval: () => {}, console,
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const evaluate = (code) => vm.runInContext(code, context);
const flush = () => new Promise((resolve) => setImmediate(resolve));
const change = (id) => {
  element("collection-select").value = id;
  element("collection-select").onchange({ target: element("collection-select") });
};
const resolveVersion = (entry, id) => entry.resolve(response({ index_versions: [version(id, entry.key)] }));
const fragments = (text) => ({ fragments: [{
  rank: 1, score: 0.5, text, metadata: { section_path: "Body", source_label: "source.md", language: "en" },
}] });
const comparison = (text) => ({ strategies: [{ strategy: text, status: "ready", counts: { chunks: 1 }, metrics: {} }] });

(async () => {
  await flush();
  assert.equal(evaluate("state.collectionId"), "a");
  assert.ok(element("versions").textContent.includes("version-"));
  evaluate('state.sources = [{path: "queued.md"}]');
  delay = true;

  change("b");
  assert.equal(element("versions").querySelector("tbody").textContent, "");
  element("chunks-active").checked = false;
  await evaluate("loadChunks()");
  assert.deepEqual(chunkRequests, [], "switching collection must not load the previous versions while refresh is pending");
  change("a");
  change("b");
  assert.equal(queues.versions.length, 3);
  resolveVersion(queues.versions[2], "version-b");
  await flush();
  resolveVersion(queues.versions[1], "version-a-late");
  resolveVersion(queues.versions[0], "version-b-stale");
  await flush();
  assert.equal(evaluate("state.versions[0].index_version_id"), "version-b");
  assert.ok(!element("versions").textContent.includes("late") && !element("versions").textContent.includes("stale"));

  evaluate('state.versions.push({collection_id:"a", index_version_id:"foreign-a", status:"ready", strategy:"fixed"})');
  await evaluate("loadChunks()");
  assert.deepEqual(chunkRequests, ["version-b"], "all-ready chunk loading must remain scoped to collection B");

  const oldSearch = evaluate("search()");
  const oldCompare = evaluate("loadCompare()");
  element("progress-text").textContent = "B-old progress";
  element("chunk-detail-body").textContent = "B-old detail";
  change("a");
  for (const id of ["fragments", "compare-result", "progress-text", "chunk-detail-body"]) {
    assert.equal(element(id).textContent, "", "switch must clear " + id);
  }
  assert.ok(element("progress-wrap").className.includes("hidden"));
  assert.ok(element("chunk-detail").className.includes("hidden"));
  assert.equal(evaluate("state.sources.length"), 1, "explicit source queue must be retained");
  assert.equal(element("search").disabled, false);
  const newSearch = evaluate("search()");
  const newCompare = evaluate("loadCompare()");
  queues.search[1].resolve(response(fragments("A-only result")));
  queues.compare[1].resolve(response(comparison("A-only metrics")));
  await Promise.all([newSearch, newCompare]);
  queues.search[0].resolve(response(fragments("B-late result")));
  queues.compare[0].resolve(response(comparison("B-late metrics")));
  await Promise.all([oldSearch, oldCompare]);
  assert.ok(element("fragments").textContent.includes("A-only result"));
  assert.ok(!element("fragments").textContent.includes("B-late result"));
  assert.ok(element("compare-result").textContent.includes("A-only metrics"));
  assert.ok(!element("compare-result").textContent.includes("B-late metrics"));

  delay = false;
  element("collection-name").value = "Created";
  await element("collection-create").onclick();
  assert.equal(evaluate("state.collectionId"), "c");
  assert.equal(element("fragments").textContent, "");
  assert.equal(element("compare-result").textContent, "");
  assert.equal(evaluate("state.sources.length"), 1);
  health = { embedding: { reachable: true, model_present: false, model: "missing-model", hint: "ollama pull missing-model" } };
  await evaluate("refreshHealth()");
  assert.ok(element("health").textContent.includes("model absent"));
  assert.ok(element("health").textContent.includes("ollama pull missing-model"));
  assert.ok(element("health").className.includes("bad"));
  health = { embedding: { reachable: false, model_present: false, model: "missing-model", hint: "ollama pull missing-model" } };
  await evaluate("refreshHealth()");
  assert.ok(element("health").textContent.includes("unreachable"));
  assert.ok(!element("health").textContent.includes("model absent"));
  console.log("UI_COLLECTION_ISOLATION: PASS (delayed versions/search/compare, all-ready scoping, cleared panels, retained source queue)");
})().catch((error) => { console.error(error); process.exitCode = 1; });

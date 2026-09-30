"use strict";

const state = {
  collections: [],
  collectionId: null,
  versions: [],
  sources: [],
  activeVersionId: null,
  polling: null,
  searching: false,
  chunkLoadId: 0,
  versionsRequestId: 0,
  collectionsRequestId: 0,
  searchRequestId: 0,
  compareRequestId: 0,
};

const $ = (id) => document.getElementById(id);

function showError(message) {
  const box = $("error");
  if (!message) {
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  box.textContent = message;
  box.classList.remove("hidden");
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = payload.error || {};
    const details = error.details ? " " + JSON.stringify(error.details) : "";
    throw new Error(`${error.code || response.status}: ${error.message || "request failed"}${details}`);
  }
  return payload;
}

function selectedVersion() {
  const select = $("collection-select");
  const collection = state.collections.find((c) => c.collection_id === select.value);
  return collection ? collection.active_index_version_id : null;
}

async function refreshHealth() {
  const health = await api("/api/health");
  const embedding = health.embedding || {};
  const badge = $("health");
  if (embedding.reachable && embedding.model_present === false) {
    badge.textContent = "Embedding model absent (" + embedding.model + ") — run: " + (embedding.hint || "ollama pull " + embedding.model);
    badge.className = "health bad";
  } else if (embedding.reachable) {
    badge.textContent = `Embedding: reachable (${embedding.model}${embedding.dimension ? ", dim " + embedding.dimension : ""})`;
    badge.className = "health ok";
  } else {
    badge.textContent = `Embedding: unreachable — run: ${embedding.hint || "ollama pull embeddinggemma:300m"}`;
    badge.className = "health bad";
  }
}


function selectCollection(collectionId) {
  if (collectionId === state.collectionId) return;
  state.collectionId = collectionId;
  state.versions = [];
  state.versionsRequestId += 1;
  state.chunkLoadId += 1;
  state.searchRequestId += 1;
  state.compareRequestId += 1;
  state.searching = false;
  $("versions").querySelector("tbody").innerHTML = "";
  $("fragments").textContent = "";
  $("compare-result").textContent = "";
  $("chunks").textContent = "Load chunks for the selected collection.";
  $("chunk-detail").classList.add("hidden");
  $("chunk-detail-body").textContent = "";
  $("progress-wrap").classList.add("hidden");
  $("progress-text").textContent = "";
  $("progress-bar").style.width = "0%";
  $("search").disabled = false;
  $("search").textContent = "Search";
  if (state.polling) clearInterval(state.polling);
  state.polling = null;
  showError("");
}
async function refreshCollections() {
  const requestId = ++state.collectionsRequestId;
  const data = await api("/api/collections");
  if (requestId !== state.collectionsRequestId) return;
  state.collections = data.collections || [];
  const select = $("collection-select");
  select.innerHTML = "";
  for (const collection of state.collections) {
    const option = document.createElement("option");
    option.value = collection.collection_id;
    option.textContent = collection.name;
    select.appendChild(option);
  }
  if (state.collections.length && !state.collections.some((c) => c.collection_id === state.collectionId)) {
    selectCollection(state.collections[0].collection_id);
  }
  if (state.collectionId) select.value = state.collectionId;
  await refreshVersions();
}

async function refreshVersions() {
  const collectionId = state.collectionId;
  const requestId = ++state.versionsRequestId;
  let versions = [];
  if (collectionId) {
    const data = await api("/api/collections/" + encodeURIComponent(collectionId) + "/index-versions");
    versions = data.index_versions || [];
  }
  if (collectionId !== state.collectionId || requestId !== state.versionsRequestId) return;
  state.versions = versions.filter((version) => version.collection_id === collectionId);
  const body = $("versions").querySelector("tbody");
  body.innerHTML = "";
  for (const version of state.versions) {
    const row = document.createElement("tr");
    const active = version.index_version_id === selectedVersion();
    for (const value of [
      version.index_version_id.slice(0, 8) + "…",
      version.strategy,
      version.status,
      version.counts ? version.counts.sources : 0,
      version.counts ? version.counts.documents : 0,
      version.counts ? version.counts.sections : 0,
      version.counts ? version.counts.chunks : 0,
      active ? "yes" : "",
      "",
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.appendChild(cell);
    }
    const actions = row.lastElementChild;
    if (version.status === "ready" && !active) {
      const button = document.createElement("button");
      button.textContent = "Set active";
      button.onclick = async () => {
        try {
          await api(`/api/collections/${state.collectionId}/active-index`, {
            method: "PUT",
            body: JSON.stringify({ index_version_id: version.index_version_id }),
          });
          await refreshCollections();
        } catch (error) {
          showError(error.message);
        }
      };
      actions.appendChild(button);
    }
    body.appendChild(row);
  }
}

function renderSources() {
  const list = $("source-list");
  list.innerHTML = "";
  state.sources.forEach((source, index) => {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = source.label || source.path;
    item.appendChild(label);
    const remove = document.createElement("button");
    remove.textContent = "Remove";
    remove.onclick = () => {
      state.sources.splice(index, 1);
      renderSources();
    };
    item.appendChild(remove);
    list.appendChild(item);
  });
}

async function buildIndex() {
  if (!state.collectionId) {
    showError("Create or select a collection first.");
    return;
  }
  if (!state.sources.length) {
    showError("Add at least one source with an explicit path.");
    return;
  }
  const collectionId = state.collectionId;
  showError("");
  $("progress-wrap").classList.remove("hidden");
  try {
    const result = await api("/api/index/build", {
      method: "POST",
      body: JSON.stringify({
        collection_id: collectionId,
        sources: state.sources,
        strategy: $("strategy").value,
      }),
    });
    if (collectionId !== state.collectionId) return;
    if (result.reused) {
      $("progress-text").textContent = `Reused existing ready index ${result.index_version_id.slice(0, 8)}…`;
      $("progress-bar").style.width = "100%";
      await refreshCollections();
      return;
    }
    pollProgress(result.index_version_id, collectionId);
  } catch (error) {
    if (collectionId === state.collectionId) {
      $("progress-wrap").classList.add("hidden");
      showError(error.message);
    }
  }
}

function pollProgress(indexVersionId, collectionId) {
  if (state.polling) clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      if (collectionId !== state.collectionId) return;
      const version = await api(`/api/index-versions/${indexVersionId}`);
      if (collectionId !== state.collectionId) return;
      const progress = version.progress || {};
      $("progress-bar").style.width = `${progress.percent || 0}%`;
      const counts = version.counts || {};
      const countText = version.status === "building"
        ? "Sources " + (progress.sources_done || 0) + "/" + (progress.sources_total || 0) + " · Chunks " + (progress.chunks_total || counts.chunks || 0)
        : "Sources " + (counts.sources || 0) + " · Documents " + (counts.documents || 0) + " · Sections " + (counts.sections || 0) + " · Chunks " + (counts.chunks || 0);
      $("progress-text").textContent = version.status + " · " + (progress.stage || "") + " · " + (progress.percent || 0) + "% · " + countText;
      if (version.status !== "building") {
        clearInterval(state.polling);
        state.polling = null;
        if (version.status === "failed") showError(version.error || "Index build failed.");
        await refreshCollections();
        await refreshHealth();
      }
    } catch (error) {
      clearInterval(state.polling);
      state.polling = null;
      showError(error.message);
    }
  }, 700);
}

async function search() {
  if (state.searching) return;
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  const collectionId = state.collectionId;
  const requestId = ++state.searchRequestId;
  showError("");
  const searchStrategy = $("search-strategy").value;
  const payload = {
    collection_id: collectionId,
    query: $("query").value,
    top_k: Number($("top-k").value) || 5,
  };
  // Do not force a strategy when the collection has an active index.
  if (searchStrategy !== "active") payload.strategy = searchStrategy;
  const button = $("search");
  const buttonText = button.textContent;
  state.searching = true;
  button.disabled = true;
  button.textContent = "Searching…";
  $("fragments").textContent = "Searching for fragments…";
  try {
    const result = await api("/api/search", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (collectionId === state.collectionId && requestId === state.searchRequestId) {
      renderFragments(result.fragments || []);
    }
  } catch (error) {
    if (collectionId === state.collectionId && requestId === state.searchRequestId) showError(error.message);
  } finally {
    if (requestId === state.searchRequestId) {
      state.searching = false;
      button.disabled = false;
      button.textContent = buttonText;
    }
    refreshHealth().catch(markHealthUnavailable);
  }
}

function renderFragments(fragments) {
  const container = $("fragments");
  container.innerHTML = "";
  if (!fragments.length) {
    container.textContent = "No fragments returned.";
    return;
  }
  for (const fragment of fragments) {
    const metadata = fragment.metadata || {};
    const card = document.createElement("div");
    card.className = "fragment";
    const pages = metadata.page_start ? `pages ${metadata.page_start}–${metadata.page_end}` : "n/a";
    const meta = document.createElement("div");
    meta.className = "meta";
    const score = document.createElement("span");
    score.className = "score";
    score.textContent = "#" + fragment.rank + " · " + fragment.score;
    meta.appendChild(score);
    meta.appendChild(document.createTextNode(
      " · " + metadata.section_path + " · " + pages + " · " + metadata.source_label + " · " + metadata.language
    ));
    const text = document.createElement("div");
    text.className = "text";
    text.textContent = fragment.text;
    card.appendChild(meta);
    card.appendChild(text);
    container.appendChild(card);
  }
}

async function loadCompare() {
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  const collectionId = state.collectionId;
  const requestId = ++state.compareRequestId;
  showError("");
  try {
    const result = await api(`/api/compare?collection_id=${encodeURIComponent(collectionId)}`);
    if (collectionId === state.collectionId && requestId === state.compareRequestId) {
      renderCompare(result.strategies || [], result);
    }
  } catch (error) {
    if (collectionId === state.collectionId && requestId === state.compareRequestId) showError(error.message);
  }
}

function renderCompare(rows, result) {
  const container = $("compare-result");
  if (!rows.length) {
    container.textContent = "No ready indexes to compare.";
    return;
  }
  container.innerHTML = "";
  if (result && result.comparable === false && result.note) {
    const warning = document.createElement("div");
    warning.className = "fragment";
    warning.textContent = `Not directly comparable: ${result.note}`;
    container.appendChild(warning);
  }
  const table = document.createElement("table");
  const header = document.createElement("tr");
  for (const title of [
    "Strategy", "Status", "Chunks", "Token min/median/p95/max",
    "Overlap overhead", "Section crossing", "Build seconds",
  ]) {
    const cell = document.createElement("th");
    cell.textContent = title;
    header.appendChild(cell);
  }
  const head = document.createElement("thead");
  head.appendChild(header);
  table.appendChild(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const metrics = row.metrics || {};
    const tokens = metrics.chunk_tokens || {};
    const line = document.createElement("tr");
    for (const value of [
      row.strategy, row.status, row.counts ? row.counts.chunks : 0,
      [tokens.min, tokens.median, tokens.p95, tokens.max].join("/"),
      metrics.overlap_overhead ?? "n/a",
      metrics.section_crossing_ratio ?? "n/a",
      metrics.build_seconds ?? "n/a",
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      line.appendChild(cell);
    }
    body.appendChild(line);
  }
  table.appendChild(body);
  container.appendChild(table);
}

function appendChunk(container, chunk) {
  const row = document.createElement("div");
  row.className = "fragment chunk-row";
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = chunk.metadata.section_path + " · tokens " + chunk.token_count + " · chars " + chunk.char_count;
  const text = document.createElement("div");
  text.className = "text";
  text.textContent = chunk.text.slice(0, 400);
  row.appendChild(meta);
  row.appendChild(text);
  row.onclick = () => {
    $("chunk-detail").classList.remove("hidden");
    $("chunk-detail-body").textContent = JSON.stringify(chunk, null, 2);
  };
  container.appendChild(row);
}

async function createChunkGroup(container, version, loadId) {
  const group = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = version.strategy + " · " + version.index_version_id.slice(0, 8) + "…";
  group.appendChild(title);
  const status = document.createElement("div");
  status.className = "meta";
  group.appendChild(status);
  const items = document.createElement("div");
  group.appendChild(items);
  const more = document.createElement("button");
  more.textContent = "Load more";
  more.className = "hidden";
  group.appendChild(more);
  container.appendChild(group);
  let offset = 0;

  async function nextPage() {
    more.disabled = true;
    status.textContent = "Loading chunks…";
    try {
      const page = await api(
        "/api/index-versions/" + encodeURIComponent(version.index_version_id) + "/chunks?limit=50&offset=" + offset
      );
      if (loadId !== state.chunkLoadId) return;
      for (const chunk of page.items) appendChunk(items, chunk);
      offset += page.items.length;
      status.textContent = "Showing " + offset + " of " + page.total + " chunks.";
      more.classList.toggle("hidden", offset >= page.total || !page.items.length);
      more.textContent = "Load more (" + Math.max(0, page.total - offset) + " remaining)";
      if (!page.total) items.textContent = "No chunks.";
    } catch (error) {
      status.textContent = "Chunk page could not be loaded.";
      showError(error.message);
      throw error;
    } finally {
      more.disabled = false;
    }
  }
  more.onclick = () => nextPage().catch(() => {});
  await nextPage();
}

async function loadChunks() {
  const loadId = ++state.chunkLoadId;
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  const activeOnly = $("chunks-active").checked;
  const active = selectedVersion();
  const versions = state.versions.filter(
    (version) => version.collection_id === state.collectionId && version.status === "ready" && (!activeOnly || version.index_version_id === active)
  );
  if (!versions.length) {
    $("chunks").textContent = activeOnly ? "No active ready index." : "No ready indexes in this collection.";
    return;
  }
  showError("");
  const button = $("load-chunks");
  button.disabled = true;
  $("chunks").innerHTML = "";
  $("chunk-detail").classList.add("hidden");
  try {
    for (const version of versions) {
      if (loadId !== state.chunkLoadId) return;
      await createChunkGroup($("chunks"), version, loadId);
    }
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

function wire() {
  $("refresh").onclick = () => Promise.all([refreshCollections(), refreshHealth()]).catch((e) => showError(e.message));
  $("collection-select").onchange = (event) => {
    selectCollection(event.target.value);
    refreshVersions().catch((e) => showError(e.message));
  };
  $("collection-create").onclick = async () => {
    const name = $("collection-name").value.trim();
    if (!name) return;
    try {
      const collection = await api("/api/collections", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      selectCollection(collection.collection_id);
      $("collection-name").value = "";
      await refreshCollections();
    } catch (error) {
      showError(error.message);
    }
  };
  $("add-source").onclick = () => {
    const path = $("source-path").value.trim();
    if (!path) return;
    state.sources.push({ path, label: $("source-label").value.trim() || undefined });
    $("source-path").value = "";
    $("source-label").value = "";
    renderSources();
  };
  $("build").onclick = () => buildIndex();
  $("search").onclick = () => search();
  $("compare").onclick = () => loadCompare();
  $("load-chunks").onclick = () => loadChunks();
  $("chunks-active").onchange = () => loadChunks();
}

function markHealthUnavailable() {
  const badge = $("health");
  badge.textContent = "Embedding: unreachable — check the backend and local model.";
  badge.className = "health bad";
}

wire();
refreshHealth().catch(markHealthUnavailable);
setInterval(() => refreshHealth().catch(markHealthUnavailable), 10000);
refreshCollections().catch((e) => showError(e.message));

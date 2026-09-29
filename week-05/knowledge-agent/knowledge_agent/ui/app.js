"use strict";

const state = {
  collections: [],
  collectionId: null,
  versions: [],
  sources: [],
  activeVersionId: null,
  polling: null,
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
  if (embedding.reachable) {
    badge.textContent = `Embedding: reachable (${embedding.model}${embedding.dimension ? ", dim " + embedding.dimension : ""})`;
    badge.className = "health ok";
  } else {
    badge.textContent = `Embedding: unreachable — run: ${embedding.hint || "ollama pull embeddinggemma:300m"}`;
    badge.className = "health bad";
  }
}

async function refreshCollections() {
  const data = await api("/api/collections");
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
    state.collectionId = state.collections[0].collection_id;
  }
  if (state.collectionId) select.value = state.collectionId;
  await refreshVersions();
}

async function refreshVersions() {
  if (!state.collectionId) {
    state.versions = [];
  } else {
    const data = await api(`/api/collections/${state.collectionId}/index-versions`);
    state.versions = data.index_versions || [];
  }
  const body = $("versions").querySelector("tbody");
  body.innerHTML = "";
  for (const version of state.versions) {
    const row = document.createElement("tr");
    const active = version.index_version_id === selectedVersion();
    row.innerHTML = `
      <td>${version.index_version_id.slice(0, 8)}…</td>
      <td>${version.strategy}</td>
      <td>${version.status}</td>
      <td>${version.counts ? version.counts.chunks : 0}</td>
      <td>${active ? "yes" : ""}</td>
      <td></td>`;
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
    item.innerHTML = `<span>${source.label || source.path}</span>`;
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
  showError("");
  $("progress-wrap").classList.remove("hidden");
  try {
    const result = await api("/api/index/build", {
      method: "POST",
      body: JSON.stringify({
        collection_id: state.collectionId,
        sources: state.sources,
        strategy: $("strategy").value,
      }),
    });
    if (result.reused) {
      $("progress-text").textContent = `Reused existing ready index ${result.index_version_id.slice(0, 8)}…`;
      $("progress-bar").style.width = "100%";
      await refreshCollections();
      return;
    }
    pollProgress(result.index_version_id);
  } catch (error) {
    $("progress-wrap").classList.add("hidden");
    showError(error.message);
  }
}

function pollProgress(indexVersionId) {
  if (state.polling) clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const version = await api(`/api/index-versions/${indexVersionId}`);
      const progress = version.progress || {};
      $("progress-bar").style.width = `${progress.percent || 0}%`;
      $("progress-text").textContent = `${version.status} · ${progress.stage || ""} · ${progress.percent || 0}%`;
      if (version.status !== "building") {
        clearInterval(state.polling);
        state.polling = null;
        await refreshCollections();
      }
    } catch (error) {
      clearInterval(state.polling);
      state.polling = null;
      showError(error.message);
    }
  }, 700);
}

async function search() {
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  showError("");
  const searchStrategy = $("search-strategy").value;
  const payload = {
    collection_id: state.collectionId,
    query: $("query").value,
    top_k: Number($("top-k").value) || 5,
  };
  // Do not force a strategy when the collection has an active index.
  if (searchStrategy !== "active") payload.strategy = searchStrategy;
  try {
    const result = await api("/api/search", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderFragments(result.fragments || []);
  } catch (error) {
    showError(error.message);
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
    card.innerHTML = `
      <div class="meta">
        <span class="score">#${fragment.rank} · ${fragment.score}</span>
        · ${metadata.section_path} · ${pages} · ${metadata.source_label} · ${metadata.language}
      </div>
      <div class="text"></div>`;
    card.querySelector(".text").textContent = fragment.text;
    container.appendChild(card);
  }
}

async function loadCompare() {
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  showError("");
  try {
    const result = await api(`/api/compare?collection_id=${encodeURIComponent(state.collectionId)}`);
    renderCompare(result.strategies || [], result);
  } catch (error) {
    showError(error.message);
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
  let html = "<table><thead><tr><th>Strategy</th><th>Status</th><th>Chunks</th>" +
    "<th>Token min/median/p95/max</th><th>Overlap overhead</th><th>Section crossing</th>" +
    "<th>Build seconds</th></tr></thead><tbody>";
  for (const row of rows) {
    const metrics = row.metrics || {};
    const tokens = metrics.chunk_tokens || {};
    html += `<tr><td>${row.strategy}</td><td>${row.status}</td>` +
      `<td>${row.counts ? row.counts.chunks : 0}</td>` +
      `<td>${tokens.min}/${tokens.median}/${tokens.p95}/${tokens.max}</td>` +
      `<td>${metrics.overlap_overhead ?? "n/a"}</td>` +
      `<td>${metrics.section_crossing_ratio ?? "n/a"}</td>` +
      `<td>${metrics.build_seconds ?? "n/a"}</td></tr>`;
  }
  container.insertAdjacentHTML("beforeend", html + "</tbody></table>");
}

async function loadChunks() {
  const indexVersionId = selectedVersion();
  if (!indexVersionId) {
    showError("No active index for this collection.");
    return;
  }
  showError("");
  try {
    const result = await api(`/api/index-versions/${indexVersionId}/chunks?limit=50`);
    const container = $("chunks");
    container.innerHTML = "";
    for (const chunk of result.items) {
      const row = document.createElement("div");
      row.className = "fragment chunk-row";
      row.innerHTML = `
        <div class="meta">${chunk.metadata.section_path} · tokens ${chunk.token_count} · chars ${chunk.char_count}</div>
        <div class="text"></div>`;
      row.querySelector(".text").textContent = chunk.text.slice(0, 400);
      row.onclick = () => {
        $("chunk-detail").classList.remove("hidden");
        $("chunk-detail-body").textContent = JSON.stringify(chunk, null, 2);
      };
      container.appendChild(row);
    }
    if (!result.items.length) container.textContent = "No chunks.";
  } catch (error) {
    showError(error.message);
  }
}

function wire() {
  $("refresh").onclick = () => refreshCollections().catch((e) => showError(e.message));
  $("collection-select").onchange = (event) => {
    state.collectionId = event.target.value;
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
      state.collectionId = collection.collection_id;
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
}

wire();
refreshHealth().catch(() => {});
refreshCollections().catch((e) => showError(e.message));

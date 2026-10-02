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
  chatRequestId: 0,
  chatBusy: false,
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
  state.chatRequestId += 1;
  state.searching = false;
  $("versions").querySelector("tbody").innerHTML = "";
  $("fragments").textContent = "";
  $("compare-result").textContent = "";
  clearChatOutput();
  $("chat-status").textContent = "";
  setChatBusy(false);
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

function setChatBusy(busy) {
  state.chatBusy = busy;
  for (const id of ["chat-plain", "chat-rag", "chat-compare", "d23-ask", "d23-compare"]) {
    $(id).disabled = busy;
  }
}

function clearChatOutput() {
  $("chat-answer").textContent = "";
  $("chat-sources").textContent = "";
  $("chat-grounding").textContent = "";
  $("chat-compare-result").textContent = "";
  $("chat-search-query").textContent = "";
  $("d23-trace").textContent = "";
  $("d23-compare-result").textContent = "";
}

async function showFragment(indexVersionId, chunkId, target) {
  target.textContent = "Loading fragment…";
  try {
    const page = await api(
      "/api/index-versions/" + encodeURIComponent(indexVersionId) +
        "/chunks?chunk_id=" + encodeURIComponent(chunkId)
    );
    if (!page.items || !page.items.length) {
      target.textContent = "Fragment is not available in this index.";
      return;
    }
    target.textContent = page.items[0].text || "";
  } catch (error) {
    target.textContent = "Fragment could not be loaded.";
    showError(error.message);
  }
}

function groundingStateText(answer, grounding) {
  if (answer.insufficient_sources) {
    const reason = grounding && grounding.refusal ? grounding.refusal.reason : "insufficient";
    return {
      warn: true,
      text: `No relevant sources found: not available in the provided documents (${reason}).`,
    };
  }
  if (grounding && grounding.status === "failed") {
    return {
      warn: true,
      text: `Verification failed${grounding.reason ? ": " + grounding.reason : ""}.`,
    };
  }
  if (grounding) {
    return {
      warn: false,
      text: `Verification status: ${grounding.status}${grounding.reason ? " (" + grounding.reason + ")" : ""}.`,
    };
  }
  return { warn: false, text: "Grounding is disabled for this answer." };
}

function renderGrounding(record) {
  const container = $("chat-grounding");
  if (!container) return;
  container.textContent = "";
  const answer = record.answer || {};
  const grounding = answer.grounding || null;
  const retrieval = record.retrieval || {};
  const indexVersionId = (record.index || {}).index_version_id;

  const heading = document.createElement("h3");
  heading.textContent = "Sources & citations";
  container.appendChild(heading);

  const state = groundingStateText(answer, grounding);
  const stateLine = document.createElement("div");
  stateLine.className = state.warn ? "meta warn" : "meta";
  stateLine.textContent = state.text;
  container.appendChild(stateLine);

  const meaningLine = document.createElement("div");
  meaningLine.className = "meta";
  meaningLine.textContent = `Meaning support: ${
    grounding && grounding.meaning_check && grounding.meaning_check !== "not_performed"
      ? grounding.meaning_check
      : "not checked"
  }`;
  container.appendChild(meaningLine);

  const passed = retrieval.passed || [];
  for (const item of passed) {
    const metadata = item.metadata || {};
    const source = document.createElement("div");
    source.className = "grounding-source";
    const pages = metadata.page_start ? `pages ${metadata.page_start}-${metadata.page_end}` : "pages n/a";
    source.textContent =
      `source: ${metadata.source_label || "n/a"} · section: ${metadata.section_path || "n/a"} · ` +
      `chunk_id: ${item.chunk_id} · ${pages}`;
    container.appendChild(source);
  }

  const citations = grounding ? grounding.citations || [] : [];
  for (const citation of citations) {
    const card = document.createElement("div");
    card.className = citation.status === "verified" ? "fragment citation" : "fragment citation bad";
    const meta = document.createElement("div");
    meta.className = "meta";
    const pages = citation.page_start ? ` · pages ${citation.page_start}-${citation.page_end}` : "";
    meta.textContent =
      `${citation.source || "n/a"} · ${citation.section || "n/a"} · chunk_id: ${citation.chunk_id}` +
      `${pages} · ${citation.status}`;
    card.appendChild(meta);
    const quote = document.createElement("blockquote");
    quote.className = "citation-quote";
    quote.textContent = citation.quote || "";
    card.appendChild(quote);
    if (citation.is_translation && citation.translation) {
      const translation = document.createElement("div");
      translation.className = "meta translation";
      translation.textContent = `Translation: ${citation.translation}`;
      card.appendChild(translation);
    }
    if (indexVersionId && citation.chunk_id) {
      const button = document.createElement("button");
      button.textContent = "Show fragment";
      const target = document.createElement("pre");
      target.className = "fragment-text";
      button.onclick = () => showFragment(indexVersionId, citation.chunk_id, target);
      card.appendChild(button);
      card.appendChild(target);
    }
    container.appendChild(card);
  }
}

function renderBranchGrounding(column, branch) {
  const grounding = branch.answer && branch.answer.grounding;
  if (!grounding) return;
  const line = document.createElement("div");
  line.className = "meta";
  line.textContent =
    `grounding ${grounding.status}${grounding.reason ? " (" + grounding.reason + ")" : ""} · ` +
    `meaning ${grounding.meaning_check === "not_performed" ? "not checked" : grounding.meaning_check}`;
  column.appendChild(line);
  for (const citation of grounding.citations || []) {
    const item = document.createElement("div");
    item.className = "citation-mini";
    item.textContent = `[${citation.status}] ${citation.chunk_id}: ${citation.quote}`;
    column.appendChild(item);
  }
}

function chatPayload(mode) {
  const payload = {
    question: $("chat-question").value.trim(),
    top_k: Number($("chat-top-k").value) || 5,
  };
  if (mode) payload.mode = mode;
  const maxContext = Number($("chat-max-context").value);
  if (maxContext > 0) payload.max_context_tokens = maxContext;
  if (state.collectionId) payload.collection_id = state.collectionId;
  const strategy = $("chat-strategy").value;
  if (strategy !== "active") payload.strategy = strategy;
  return payload;
}

function metadataLine(metadata) {
  const meta = metadata || {};
  const pages = meta.page_start ? `стр. ${meta.page_start}–${meta.page_end}` : "стр. н/д";
  return `${meta.section_path || "раздел н/д"} · ${pages} · ${meta.source_label || ""}`;
}

function renderChatSources(retrieval) {
  const container = $("chat-sources");
  container.textContent = "";
  if (!retrieval) return;
  const header = document.createElement("div");
  header.className = "meta";
  header.textContent = `Найдено: ${retrieval.found_count}, передано в контекст: ${retrieval.passed_count}`;
  container.appendChild(header);
  for (const item of retrieval.passed || []) {
    const card = document.createElement("div");
    card.className = "fragment";
    const meta = document.createElement("div");
    meta.className = "meta";
    const id = (item.chunk_id || "").slice(0, 8);
    meta.textContent = `#${item.rank} · ${id}… · ${metadataLine(item.metadata)} · ~${item.estimated_tokens} токенов`;
    card.appendChild(meta);
    container.appendChild(card);
  }
}

function renderChatAnswer(answer) {
  const container = $("chat-answer");
  container.textContent = "";
  const text = document.createElement("div");
  text.className = "text";
  text.textContent = answer.text || "";
  container.appendChild(text);
  const citations = answer.citations || {};
  const meta = document.createElement("div");
  meta.className = answer.truncated ? "meta warn" : "meta";
  const parts = [];
  parts.push(`finish_reason: ${answer.finish_reason || "н/д"}`);
  if (answer.truncated) parts.push("ответ обрезан по лимиту длины");
  parts.push(`цитаты: корректные ${(citations.valid || []).length}, непереданные ${(citations.unsupported || []).length}`);
  meta.textContent = parts.join(" · ");
  container.appendChild(meta);
}

function chatUsageLine(record) {
  const parts = [];
  if (record.usage) {
    parts.push(`tokens in/out: ${record.usage.input_tokens ?? "н/д"}/${record.usage.output_tokens ?? "н/д"}`);
  } else {
    parts.push("usage: н/д");
  }
  if (record.output_tokens_per_second != null) parts.push(`${record.output_tokens_per_second} tok/s`);
  if (record.latency_ms && record.latency_ms.total != null) parts.push(`задержка ${record.latency_ms.total} мс`);
  return parts.join(" · ");
}

function handleChatEvent(event) {
  if (event.type === "start") {
    clearChatOutput();
    const model = event.model && event.model.model ? event.model.model : "н/д";
    $("chat-status").textContent = `Прогон ${String(event.run_id).slice(0, 16)}… · режим ${event.mode} · модель ${model}`;
  } else if (event.type === "sources") {
    renderChatSources({
      found_count: event.found_count,
      passed_count: event.passed_count,
      passed: event.passed,
    });
  } else if (event.type === "token") {
    const container = $("chat-answer");
    container.textContent = container.textContent + event.text;
  } else if (event.type === "done") {
    const record = event.answer || {};
    renderChatAnswer(record.answer || {});
    renderChatSources(record.retrieval);
    renderGrounding(record);
    $("chat-status").textContent = `Готово · ${chatUsageLine(record)}`;
  } else if (event.type === "error") {
    const error = event.error || {};
    const hint = error.details && error.details.hint ? ` (${error.details.hint})` : "";
    showError(`${error.code || "chat_error"}: ${error.message || "ошибка"}${hint}`);
    $("chat-status").textContent = "Ошибка";
  }
}

async function streamChatRequest(payload, requestId) {
  const response = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = data.error || {};
    const hint = error.details && error.details.hint ? ` (${error.details.hint})` : "";
    throw new Error(`${error.code || response.status}: ${error.message || "request failed"}${hint}`);
  }
  if (!response.body || typeof response.body.getReader !== "function") return false;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    if (requestId !== state.chatRequestId) return true;
    buffer += decoder.decode(chunk.value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
      if (!dataLine) continue;
      handleChatEvent(JSON.parse(dataLine.slice(5).trim()));
    }
  }
  return true;
}

async function runChat(mode) {
  if (state.chatBusy) return;
  const question = $("chat-question").value.trim();
  if (!question) {
    showError("Введите вопрос.");
    return;
  }
  if (mode === "with_rag" && !state.collectionId) {
    showError("Выберите коллекцию для режима «С RAG».");
    return;
  }
  showError("");
  const requestId = ++state.chatRequestId;
  setChatBusy(true);
  clearChatOutput();
  $("chat-status").textContent = "Ожидание ответа модели…";
  try {
    const streamed = await streamChatRequest(chatPayload(mode), requestId);
    if (requestId !== state.chatRequestId) return;
    if (!streamed) {
      const record = await api("/api/chat", { method: "POST", body: JSON.stringify(chatPayload(mode)) });
      if (requestId !== state.chatRequestId) return;
      renderChatAnswer(record.answer || {});
      renderChatSources(record.retrieval);
      renderGrounding(record);
      $("chat-status").textContent = `Готово · ${chatUsageLine(record)}`;
    }
  } catch (error) {
    if (requestId === state.chatRequestId) {
      showError(error.message);
      $("chat-status").textContent = "Ошибка";
    }
  } finally {
    if (requestId === state.chatRequestId) setChatBusy(false);
  }
}

function renderChatCompare(record) {
  const container = $("chat-compare-result");
  container.textContent = "";
  const branches = record.branches || {};
  const row = document.createElement("div");
  row.className = "compare-columns";
  for (const [mode, title] of [["with_rag", "С RAG"], ["without_rag", "Без RAG"]]) {
    const column = document.createElement("div");
    column.className = "compare-column";
    const heading = document.createElement("h3");
    heading.textContent = title;
    column.appendChild(heading);
    const branch = branches[mode] || {};
    const answer = document.createElement("div");
    answer.className = "text";
    answer.textContent = (branch.answer && branch.answer.text) || "";
    column.appendChild(answer);
    const meta = document.createElement("div");
    meta.className = "meta";
    const truncated = branch.answer && branch.answer.truncated ? " · обрезано" : "";
    const template = branch.prompt ? branch.prompt.template_id : "н/д";
    meta.textContent = `${template} · ${chatUsageLine(branch)}${truncated}`;
    column.appendChild(meta);
    if (branch.retrieval) {
      const sources = document.createElement("div");
      sources.className = "meta";
      sources.textContent = `Найдено ${branch.retrieval.found_count}, передано ${branch.retrieval.passed_count}`;
      column.appendChild(sources);
    }
    row.appendChild(column);
  }
  container.appendChild(row);
  const policy = document.createElement("div");
  policy.className = "meta";
  const comparison = record.comparison || {};
  const templates = comparison.prompt_templates || {};
  policy.textContent =
    `Одна модель: ${comparison.same_model ? "да" : "нет"} · одинаковые настройки: ` +
    `${comparison.same_settings ? "да" : "нет"} · шаблоны: С RAG ${templates.with_rag || "н/д"}, ` +
    `Без RAG ${templates.without_rag || "н/д"}`;
  container.appendChild(policy);
}

async function runChatCompare() {
  if (state.chatBusy) return;
  if (!state.collectionId) {
    showError("Выберите коллекцию для сравнения.");
    return;
  }
  if (!$("chat-question").value.trim()) {
    showError("Введите вопрос.");
    return;
  }
  showError("");
  const requestId = ++state.chatRequestId;
  setChatBusy(true);
  clearChatOutput();
  $("chat-status").textContent = "Сравнение режимов…";
  try {
    const payload = chatPayload(null);
    const record = await api("/api/chat/compare", { method: "POST", body: JSON.stringify(payload) });
    if (requestId !== state.chatRequestId) return;
    renderChatCompare(record);
    const index = record.comparison && record.comparison.index_version_id;
    $("chat-status").textContent = `Сравнение готово · индекс ${index ? String(index).slice(0, 8) + "…" : "н/д"}`;
  } catch (error) {
    if (requestId === state.chatRequestId) {
      showError(error.message);
      $("chat-status").textContent = "Ошибка";
    }
  } finally {
    if (requestId === state.chatRequestId) setChatBusy(false);
  }
}

function d23Payload() {
  const payload = {
    mode: "with_rag",
    question: $("chat-question").value.trim(),
    top_k: Number($("chat-top-k").value) || 5,
    rag_mode: $("d23-mode").value,
    min_score: Number($("d23-min-score").value),
    prefilter_top_k: Number($("d23-prefilter").value) || 20,
    postfilter_top_k: Number($("d23-postfilter").value) || 5,
  };
  const maxContext = Number($("chat-max-context").value);
  if (maxContext > 0) payload.max_context_tokens = maxContext;
  if (state.collectionId) payload.collection_id = state.collectionId;
  const strategy = $("chat-strategy").value;
  if (strategy !== "active") payload.strategy = strategy;
  return payload;
}

function renderD23Trace(record) {
  const searchQuery = $("chat-search-query");
  searchQuery.textContent = "";
  if (record.original_query && record.search_query && record.original_query !== record.search_query) {
    let text = `Search query: ${record.search_query}`;
    if (record.rewrite && record.rewrite.fallback) {
      text += ` (rewrite fallback: ${record.rewrite.reason || "unknown"})`;
    }
    searchQuery.textContent = text;
  } else if (record.use_rewrite) {
    searchQuery.textContent = `Search query: ${record.search_query || record.original_query || ""}`;
  }
  const container = $("d23-trace");
  container.textContent = "";
  const retrieval = record.retrieval;
  if (!retrieval) return;
  const reasons = retrieval.exclusion_reasons || {};
  const parts = [
    `Mode ${record.rag_mode || "-"}`,
    `candidates ${retrieval.found_count}`,
    `selected ${retrieval.selected_count}`,
    `passed ${retrieval.passed_count}`,
    `excluded threshold ${(reasons.threshold || []).length} / top_k ${(reasons.top_k || []).length} / budget ${(reasons.context_budget || []).length}`,
  ];
  const rewrite = record.rewrite || {};
  if (rewrite.attempted) {
    parts.push(`rewrite ${rewrite.used ? "used" : "fallback"} ${rewrite.latency_ms ?? "n/a"} ms`);
  }
  container.textContent = parts.join(" · ");
}

async function runD23Ask() {
  if (state.chatBusy) return;
  const question = $("chat-question").value.trim();
  if (!question) {
    showError("Enter a question.");
    return;
  }
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  showError("");
  const requestId = ++state.chatRequestId;
  setChatBusy(true);
  clearChatOutput();
  $("chat-status").textContent = "Waiting for the model…";
  try {
    const record = await api("/api/chat", { method: "POST", body: JSON.stringify(d23Payload()) });
    if (requestId !== state.chatRequestId) return;
    renderChatAnswer(record.answer || {});
    renderChatSources(record.retrieval);
    renderGrounding(record);
    renderD23Trace(record);
    $("chat-status").textContent = `Done · ${chatUsageLine(record)}`;
  } catch (error) {
    if (requestId === state.chatRequestId) {
      showError(error.message);
      $("chat-status").textContent = "Error";
    }
  } finally {
    if (requestId === state.chatRequestId) setChatBusy(false);
  }
}

function renderD23Compare(record) {
  const container = $("d23-compare-result");
  container.textContent = "";
  const comparison = record.comparison || {};
  const policy = document.createElement("div");
  policy.className = "meta";
  policy.textContent =
    `Threshold ${comparison.threshold} · prefilter ${comparison.prefilter_top_k} / postfilter ` +
    `${comparison.postfilter_top_k} · same model ${comparison.same_model ? "yes" : "no"} · ` +
    `same settings ${comparison.same_settings ? "yes" : "no"}`;
  container.appendChild(policy);
  const row = document.createElement("div");
  row.className = "compare-columns four";
  for (const mode of record.modes || []) {
    const branch = mode.branch || {};
    const column = document.createElement("div");
    column.className = "compare-column";
    const heading = document.createElement("h3");
    heading.textContent =
      `${mode.id} · filter ${mode.use_filter ? "on" : "off"} · rewrite ${mode.use_rewrite ? "on" : "off"}`;
    column.appendChild(heading);
    const answer = document.createElement("div");
    answer.className = "text";
    answer.textContent = (branch.answer && branch.answer.text) || "";
    column.appendChild(answer);
    const rewrite = branch.rewrite || {};
    const latency = branch.latency_ms || {};
    const generation = document.createElement("div");
    generation.className = "meta";
    const finishReason = branch.answer ? (branch.answer.finish_reason ?? "n/a") : "n/a";
    const truncated = branch.answer && branch.answer.truncated ? "yes" : "no";
    generation.textContent =
      `${branch.rag_mode || mode.id} · finish_reason ${finishReason} · truncated ${truncated} · ` +
      `gen ${chatUsageLine(branch)} · gen latency ${latency.chat ?? "n/a"} ms (total ${latency.total ?? "n/a"} ms)`;
    column.appendChild(generation);
    const rewriteLine = document.createElement("div");
    rewriteLine.className = "meta";
    if (rewrite.attempted) {
      const usage = rewrite.usage
        ? `in ${rewrite.usage.input_tokens}/out ${rewrite.usage.output_tokens}`
        : "usage n/a";
      rewriteLine.textContent =
        `rewrite ${rewrite.used ? "used" : "fallback"} · ${rewrite.latency_ms ?? "n/a"} ms · ${usage}` +
        (rewrite.fallback ? ` (${rewrite.reason || "unknown"})` : "");
    } else {
      rewriteLine.textContent = "rewrite off";
    }
    column.appendChild(rewriteLine);
    if (branch.retrieval) {
      const sources = document.createElement("div");
      sources.className = "meta";
      sources.textContent =
        `found ${branch.retrieval.found_count}, selected ${branch.retrieval.selected_count}, ` +
        `passed ${branch.retrieval.passed_count}`;
      column.appendChild(sources);
      const list = document.createElement("ul");
      list.className = "compare-sources";
      for (const item of branch.retrieval.passed || []) {
        const entry = document.createElement("li");
        const metadata = item.metadata || {};
        const origin = metadata.source_path || metadata.section_path || "";
        entry.textContent = `[${item.rank}] ${item.chunk_id}${origin ? " · " + origin : ""}`;
        list.appendChild(entry);
      }
      if (list.childNodes.length) column.appendChild(list);
    }
    renderBranchGrounding(column, branch);
    row.appendChild(column);
  }
  container.appendChild(row);
}

async function runD23Compare() {
  if (state.chatBusy) return;
  const question = $("chat-question").value.trim();
  if (!question) {
    showError("Enter a question.");
    return;
  }
  if (!state.collectionId) {
    showError("Select a collection first.");
    return;
  }
  showError("");
  const requestId = ++state.chatRequestId;
  setChatBusy(true);
  clearChatOutput();
  $("chat-status").textContent = "Comparing four modes…";
  try {
    const payload = {
      collection_id: state.collectionId,
      question,
      min_score: Number($("d23-min-score").value),
      prefilter_top_k: Number($("d23-prefilter").value) || 20,
      postfilter_top_k: Number($("d23-postfilter").value) || 5,
    };
    const strategy = $("chat-strategy").value;
    if (strategy !== "active") payload.strategy = strategy;
    const record = await api("/api/chat/compare-modes", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (requestId !== state.chatRequestId) return;
    renderD23Compare(record);
    $("chat-status").textContent = "Four-mode comparison ready";
  } catch (error) {
    if (requestId === state.chatRequestId) {
      showError(error.message);
      $("chat-status").textContent = "Error";
    }
  } finally {
    if (requestId === state.chatRequestId) setChatBusy(false);
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
  $("chat-plain").onclick = () => runChat("without_rag");
  $("chat-rag").onclick = () => runChat("with_rag");
  $("chat-compare").onclick = () => runChatCompare();
  $("d23-ask").onclick = () => runD23Ask();
  $("d23-compare").onclick = () => runD23Compare();
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

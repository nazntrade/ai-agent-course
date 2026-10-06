"use strict";

const state = {
  provider: "local",
  dialogueId: null,
  dialogues: [],
  busy: false,
  memoryVersion: 0,
};

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = null; }
  if (!response.ok) {
    const message = payload && payload.error ? payload.error.message : "Request failed";
    const error = new Error(message);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function setStateBadge(value) {
  const badge = document.getElementById("state-badge");
  badge.textContent = value;
  badge.className = "badge " + value;
}

function setNotice(text) {
  const notice = document.getElementById("notice");
  if (!text) { notice.hidden = true; return; }
  notice.hidden = false;
  notice.textContent = text;
}

async function refreshProvider() {
  const result = await api("/api/provider");
  state.provider = result.selected;
  document.getElementById("btn-local").classList.toggle("active", result.selected === "local");
  document.getElementById("btn-network").classList.toggle("active", result.selected === "network");
  if (state.busy) { setStateBadge("generating"); return; }
  if (result.selected === "local") {
    setStateBadge(result.local_state);
    if (result.local_error) setNotice(result.local_error);
  } else {
    setStateBadge("network");
    setNotice(result.missing_config && result.missing_config.length
      ? "Missing network configuration: " + result.missing_config.join(", ")
      : (result.network_reachable ? "" : "Network provider is not reachable"));
  }
}

async function loadDialogues() {
  const result = await api("/api/dialogues");
  state.dialogues = result.dialogues;
  const list = document.getElementById("dialogue-list");
  list.innerHTML = "";
  for (const dialogue of state.dialogues) {
    const item = document.createElement("li");
    item.classList.toggle("active", dialogue.dialogue_id === state.dialogueId);
    const name = document.createElement("span");
    name.className = "dialogue-name";
    name.textContent = dialogue.name;
    name.onclick = () => openDialogue(dialogue.dialogue_id);
    const rename = document.createElement("button");
    rename.className = "icon";
    rename.textContent = "Rename";
    rename.onclick = (event) => { event.stopPropagation(); renameDialogue(dialogue, item); };
    const remove = document.createElement("button");
    remove.className = "icon danger";
    remove.textContent = "Delete";
    remove.onclick = (event) => { event.stopPropagation(); deleteDialogue(dialogue, item); };
    const actions = document.createElement("span");
    actions.className = "dialogue-actions";
    actions.append(rename, remove);
    item.append(name, actions);
    list.appendChild(item);
  }
}

async function renameDialogue(dialogue, row) {
  const activeName = row.querySelector(".dialogue-name");
  if (!activeName) return;
  const field = document.createElement("input");
  field.className = "rename-input";
  field.setAttribute("aria-label", "Dialogue name");
  field.value = dialogue.name;
  activeName.replaceWith(field);
  field.focus(); field.select();
  let finished = false;
  async function finish(save) {
    if (finished) return;
    finished = true;
    try {
      if (save && field.value.trim()) await api("/api/dialogues/" + encodeURIComponent(dialogue.dialogue_id), {
        method: "PATCH", body: JSON.stringify({name: field.value.trim()}),
      });
    } catch (error) { setNotice(error.message); }
    await loadDialogues();
  }
  field.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    if (event.key === "Escape") finish(false);
  });
  field.addEventListener("blur", () => finish(true));
}

async function deleteDialogue(dialogue, row) {
  if (!row) return;
  const approved = await new Promise(resolve => {
    const box = document.createElement("div");
    box.className = "delete-confirmation";
    const question = document.createElement("span"); question.textContent = "Delete this dialogue?";
    const yes = document.createElement("button"); yes.className = "icon danger"; yes.textContent = "Confirm delete";
    const no = document.createElement("button"); no.className = "icon"; no.textContent = "Cancel";
    yes.onclick = () => { box.remove(); resolve(true); };
    no.onclick = () => { box.remove(); resolve(false); };
    box.append(question, yes, no); row.appendChild(box);
  });
  if (!approved) return;
  await api("/api/dialogues/" + encodeURIComponent(dialogue.dialogue_id), { method: "DELETE" });
  if (state.dialogueId === dialogue.dialogue_id) {
    state.dialogueId = null;
    renderMessages([]);
  }
  await loadDialogues();
  if (!state.dialogueId && state.dialogues.length) {
    await openDialogue(state.dialogues[0].dialogue_id);
  }
}

function renderMessages(messages) {
  const container = document.getElementById("messages");
  container.innerHTML = "";
  for (const message of messages) {
    const bubble = document.createElement("div");
    bubble.className = "msg " + (message.is_error ? "error" : message.role);
    bubble.textContent = message.is_error
      ? "Error: " + ((message.parameters && message.parameters.error_code) || "generation failed")
      : message.text;
    if (message.role === "assistant" && !message.is_error) {
      const meta = document.createElement("small");
      meta.className = "answer-meta";
      meta.textContent = "\n" + (message.model || "") + " · " + Math.round(message.latency_ms || 0) + " ms";
      bubble.appendChild(meta);
    }
    const rag = message.parameters && message.parameters.rag;
    if (rag && rag.enabled && rag.sources.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = "Sources and citation check";
      const proof = document.createElement("pre");
      proof.textContent = rag.sources.map(s => s.label + " · " + s.chunk_id + "\n“" + s.quote + "”").join("\n\n") + "\n\n" + JSON.stringify(rag.citations, null, 2);
      details.append(summary, proof); bubble.appendChild(details);
    }
    container.appendChild(bubble);
  }
  container.scrollTop = container.scrollHeight;
}

async function openDialogue(dialogueId) {
  state.dialogueId = dialogueId;
  const dialogue = await api("/api/dialogues/" + encodeURIComponent(dialogueId));
  renderMessages(dialogue.messages);
  state.memoryVersion = dialogue.memory.version;
  document.getElementById("memory-goal").value = dialogue.memory.goal || "";
  document.getElementById("memory-constraints").value = (dialogue.memory.constraints || []).join("\n");
  document.getElementById("memory-view").textContent = JSON.stringify(dialogue.memory, null, 2);
  const latest = [...dialogue.messages].reverse().find(m => m.role === "assistant" && !m.is_error);
  const rag = latest && latest.parameters && latest.parameters.rag;
  renderSources(rag ? rag.sources : []);
  document.getElementById("citation-view").textContent = rag && rag.citations ? JSON.stringify(rag.citations, null, 2) : "";
  await loadDialogues();
}

async function selectProvider(provider) {
  if (state.switching) return;
  state.switching = true;
  const controls = ["btn-local", "btn-network", "btn-unload", "btn-send"].map(id => document.getElementById(id));
  controls.forEach(control => { control.disabled = true; });
  try {
    setStateBadge(provider === "local" ? "loading" : "unloaded");
    await api("/api/provider", {
      method: "POST",
      body: JSON.stringify({ provider, dialogue_id: state.dialogueId }),
    });
    if (provider === "local") await api("/api/local/load", { method: "POST" });
  } catch (error) {
    setNotice(error.message);
  } finally {
    state.switching = false;
    controls.forEach(control => { control.disabled = false; });
    await refreshProvider();
  }
}

async function send() {
  if (state.busy) return;
  if (!state.dialogueId) {
    const created = await api("/api/dialogues", { method: "POST", body: "{}" });
    state.dialogueId = created.dialogue_id;
    await loadDialogues();
  }
  const input = document.getElementById("input");
  const question = input.value.trim();
  const requestDialogueId = state.dialogueId;
  if (!question) return;
  state.busy = true;
  setStateBadge("generating");
  try {
    const ragEnabled = document.getElementById("rag-toggle").checked;
    const result = await api("/api/ask", {
      method: "POST",
      body: JSON.stringify({
        dialogue_id: requestDialogueId,
        question,
        use_rewrite: document.getElementById("search-rewrite").checked,
        min_score: document.getElementById("search-min-score").value === "" ? null : Number(document.getElementById("search-min-score").value),
        rag_enabled: ragEnabled,
        provider: state.provider,
      }),
    });
    if (state.dialogueId !== requestDialogueId) return;
    if (input.value.trim() === question) { input.value = ""; input.style.height = "auto"; }
    await openDialogue(requestDialogueId);
    renderSources(result.rag.enabled ? result.rag.sources : []);
    document.getElementById("citation-view").textContent =
      result.rag.citations ? JSON.stringify(result.rag.citations, null, 2) : "";
  } catch (error) {
    setNotice(error.message);
    await openDialogue(state.dialogueId);
  } finally {
    state.busy = false;
    await refreshProvider();
  }
}

function renderSources(sources) {
  const panel = document.getElementById("sources");
  panel.innerHTML = "";
  for (const source of sources) {
    const item = document.createElement("div");
    item.className = "source-item";
    item.innerHTML = '<span class="label"></span> <span class="score"></span>';
    item.querySelector(".label").textContent = source.label || source.source || "source";
    item.querySelector(".score").textContent = source.score != null ? "(" + source.score + ")" : "";
    const details = document.createElement("div");
    details.textContent = "chunk_id: " + source.chunk_id + (source.quote ? "\n“" + source.quote + "”" : "");
    item.appendChild(details);
    panel.appendChild(item);
  }
}

async function indexDocument() {
  const label = document.getElementById("doc-label").value.trim();
  const text = document.getElementById("doc-text").value.trim();
  if (!label || !text) return;
  try {
    await api("/api/rag/documents", { method: "POST", body: JSON.stringify({ label, text }) });
    setNotice("Document indexed: " + label);
  } catch (error) {
    setNotice(error.message);
  }
}

function renderResults(container, results) {
  container.innerHTML = "";
  for (const item of results) {
    const row = document.createElement("div");
    row.className = "source-item";
    row.textContent = (item.label || "source") + " — " + item.score + ": " + item.text.slice(0, 120);
    container.appendChild(row);
  }
}

function searchParams() {
  const params = new URLSearchParams();
  params.set("query", document.getElementById("search-query").value.trim());
  if (document.getElementById("search-rewrite").checked) params.set("use_rewrite", "true");
  const minScore = document.getElementById("search-min-score").value;
  if (minScore !== "") params.set("min_score", minScore);
  return params;
}

async function search() {
  const container = document.getElementById("search-results");
  if (!document.getElementById("search-query").value.trim()) return;
  try {
    const result = await api("/api/rag/search?" + searchParams().toString());
    container.dataset.searchQuery = result.search_query;
    renderResults(container, result.results);
  } catch (error) {
    container.textContent = error.message;
  }
}

async function compare() {
  const container = document.getElementById("search-results");
  if (!document.getElementById("search-query").value.trim()) return;
  try {
    const result = await api("/api/rag/compare?" + searchParams().toString());
    container.innerHTML = "";
    const summary = document.createElement("div");
    summary.className = "source-item";
    summary.textContent = "unfiltered=" + result.unfiltered.length + " filtered=" + result.filtered.length + " stable=" + result.stable;
    container.appendChild(summary);
    const filteredHeader = document.createElement("div");
    filteredHeader.className = "source-item";
    filteredHeader.textContent = "Filtered results:";
    container.appendChild(filteredHeader);
    for (const item of result.filtered) {
      const row = document.createElement("div");
      row.className = "source-item";
      row.textContent = (item.label || "source") + " — " + item.score + ": " + item.text.slice(0, 100);
      container.appendChild(row);
    }
  } catch (error) {
    container.textContent = error.message;
  }
}

function wire() {
  document.getElementById("btn-local").onclick = () => selectProvider("local");
  document.getElementById("btn-network").onclick = () => selectProvider("network");
  document.getElementById("btn-unload").onclick = async () => {
    try { await api("/api/local/unload", { method: "POST" }); } catch (error) { setNotice(error.message); }
    await refreshProvider();
  };
  document.getElementById("btn-new-dialogue").onclick = async () => {
    const created = await api("/api/dialogues", { method: "POST", body: "{}" });
    await loadDialogues();
    await openDialogue(created.dialogue_id);
  };
  document.getElementById("btn-send").onclick = send;
  document.getElementById("btn-save-memory").onclick = async () => {
    if (!state.dialogueId) { setNotice("Create a dialogue first."); return; }
    try {
      await api("/api/dialogues/" + encodeURIComponent(state.dialogueId) + "/memory", {
        method: "PATCH", body: JSON.stringify({expected_version: state.memoryVersion, memory: {
          goal: document.getElementById("memory-goal").value.trim(),
          constraints: document.getElementById("memory-constraints").value.split("\n").map(s => s.trim()).filter(Boolean),
        }}),
      });
      await openDialogue(state.dialogueId);
      setNotice("Task memory saved.");
    } catch (error) { setNotice(error.message); }
  };
  document.getElementById("btn-index").onclick = indexDocument;
  document.getElementById("btn-search").onclick = search;
  document.getElementById("btn-compare").onclick = compare;
  document.getElementById("btn-toggle-panel").onclick = () => {
    const layout = document.querySelector(".layout");
    layout.classList.toggle("panel-hidden");
    document.getElementById("btn-toggle-panel").textContent =
      layout.classList.contains("panel-hidden") ? "Show settings" : "Hide settings";
    document.getElementById("btn-toggle-panel").setAttribute("aria-expanded", String(!layout.classList.contains("panel-hidden")));
  };
  const input = document.getElementById("input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });
  document.getElementById("rag-toggle").addEventListener("change", (event) => {
    document.getElementById("rag-panel").hidden = !event.target.checked;
    document.getElementById("rag-tools").hidden = !event.target.checked;
  });
}

async function init() {
  wire();
  await refreshProvider();
  await loadDialogues();
  state.dialogueId = state.dialogues.length ? state.dialogues[0].dialogue_id : null;
  if (state.dialogueId) await openDialogue(state.dialogueId);
  setInterval(() => refreshProvider().catch(() => { setStateBadge("error"); setNotice("The application connection was lost. Retrying…"); }), 4000);
}

init();

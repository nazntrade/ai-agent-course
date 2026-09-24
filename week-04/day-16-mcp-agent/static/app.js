"use strict";

// Relative base: the UI and the API share one origin. It stays empty so the
// same build works behind a reverse proxy later, and it never carries a
// credential, an Authorization header or an upstream provider key.
const API_BASE = "";

// The MCP tool whose slow external call drives the spinning-globe indicator.
const WEB_SEARCH_TOOL = "search_web";
const SVG_NS = "http://www.w3.org/2000/svg";

const MAX_RENDERED_TASK_LINKS = 5;
// The scheduled-tasks panel refreshes itself while the page is open. The poll
// never calls a tool or the model; it only re-reads the existing tasks endpoint.
const TASKS_POLL_INTERVAL_MS = 10000;
const DEFAULT_CHAT_TITLE = "New chat";
const STORAGE_KEY = "day18.activeChatId";
// Fallback shown only when the backend 409 does not carry a message of its own.
const CHAT_LIMIT_FALLBACK =
  "The limit of 5 chats is reached. Delete a chat to create a new one.";

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const sendButton = document.getElementById("send-button");
const clearButton = document.getElementById("clear-button");
const refreshButton = document.getElementById("refresh-button");
const mcpStatusEl = document.getElementById("mcp-status");
const chatsListEl = document.getElementById("chats-list");
const newChatButton = document.getElementById("new-chat-button");
const chatLimitMessageEl = document.getElementById("chat-limit-message");
const tasksListEl = document.getElementById("tasks-list");
const tasksRefreshButton = document.getElementById("tasks-refresh-button");

// The selected chat is the only context a request may use. It is an opaque id
// and is never written into visible text.
const state = {
  chats: [],
  activeChatId: null,
  busy: false,
};
let selectionToken = 0;
// Only one tasks request may be in flight at a time, and the timer is a single
// handle so that starting the poll twice never creates a second interval.
let tasksPollTimer = null;
let tasksRequestInFlight = false;

function addBubble(kind, text) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${kind}`;
  const body = document.createElement("span");
  body.className = "text";
  body.textContent = text || "";
  bubble.appendChild(body);
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function addLoader() {
  const bubble = document.createElement("div");
  bubble.className = "bubble assistant";
  const loader = document.createElement("span");
  loader.className = "loader";
  loader.innerHTML = "<span></span><span></span><span></span>";
  bubble.appendChild(loader);
  const stage = document.createElement("span");
  stage.className = "stage";
  bubble.appendChild(stage);
  messagesEl.appendChild(bubble);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function removeLoader(bubble) {
  const loader = bubble.querySelector(".loader");
  if (loader) {
    loader.remove();
  }
  const stage = bubble.querySelector(".stage");
  if (stage) {
    stage.remove();
  }
}

function setStage(bubble, text) {
  const stage = bubble.querySelector(".stage");
  if (stage) {
    stage.textContent = text;
  }
}

// A small spinning globe is shown only while the web-search tool is running.
// It is created lazily inside the answer bubble and toggled by the `active`
// class, so consecutive searches reuse one element.
function createGlobeIcon() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const outline = document.createElementNS(SVG_NS, "circle");
  outline.setAttribute("cx", "12");
  outline.setAttribute("cy", "12");
  outline.setAttribute("r", "10");
  const meridian = document.createElementNS(SVG_NS, "ellipse");
  meridian.setAttribute("cx", "12");
  meridian.setAttribute("cy", "12");
  meridian.setAttribute("rx", "4.5");
  meridian.setAttribute("ry", "10");
  const equator = document.createElementNS(SVG_NS, "path");
  equator.setAttribute("d", "M2 12h20");
  svg.appendChild(outline);
  svg.appendChild(meridian);
  svg.appendChild(equator);
  return svg;
}

function searchSpinner(bubble) {
  return bubble.querySelector(":scope > .search-spinner");
}

function showSearchSpinner(bubble) {
  let spinner = searchSpinner(bubble);
  if (!spinner) {
    spinner = document.createElement("span");
    spinner.className = "search-spinner";
    spinner.setAttribute("role", "status");
    spinner.setAttribute("aria-label", "Searching the web");
    spinner.appendChild(createGlobeIcon());
    bubble.appendChild(spinner);
  }
  spinner.classList.add("active");
}

function hideSearchSpinner(bubble) {
  const spinner = searchSpinner(bubble);
  if (spinner) {
    spinner.classList.remove("active");
  }
}

// One collapsed Technical details block per answer. Progress lines and the raw
// tool-call arguments live only here, never in the streamed answer text.
function technicalBlock(bubble) {
  let details = bubble.querySelector("details.technical");
  if (!details) {
    details = document.createElement("details");
    details.className = "technical";
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    details.appendChild(summary);
    bubble.appendChild(details);
  }
  return details;
}

function technicalLine(bubble, text, bad) {
  const details = technicalBlock(bubble);
  const line = document.createElement("div");
  line.className = bad ? "tool-line bad" : "tool-line";
  line.textContent = text;
  details.appendChild(line);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addSystem(text) {
  return addBubble("system", text);
}

function describeError(event) {
  const category = event.category || "error";
  const message = event.message || "The request failed.";
  // The backend message is the source of truth. A hint is only appended where
  // it cannot contradict the category.
  const hints = {
    model_not_configured:
      "Set the key in .env (copy .env.example) and try again.",
    // The backend message already states that the MCP server is unreachable
    // (with its loopback host:port), so the hint only adds the recovery step;
    // repeating "not reachable" would duplicate the same sentence.
    mcp_unavailable: "Start it (run_app.bat) and try again.",
  };
  const hint = hints[category];
  return hint ? `${message} ${hint}` : message;
}

function failBubble(bubble, text) {
  removeLoader(bubble);
  hideSearchSpinner(bubble);
  let body = bubble.querySelector(".text");
  if (!body) {
    body = document.createElement("span");
    body.className = "text";
    bubble.insertBefore(body, bubble.firstChild);
  }
  body.textContent = text;
  bubble.classList.add("error");
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function parseEventBlock(block) {
  const lines = block.split("\n");
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) {
    return null;
  }
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

async function sendMessage(message, bubble) {
  const response = await fetch(`${API_BASE}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: state.activeChatId || "",
      message,
    }),
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    if (response.status === 422) {
      detail = "The message must not be empty.";
    }
    failBubble(bubble, `The backend rejected the request: ${detail}`);
    return;
  }
  if (!response.body) {
    failBubble(bubble, "This browser cannot read a streaming response.");
    return;
  }

  let started = false;
  let rawText = "";
  let textNode = null;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const beginText = () => {
    if (started) {
      return;
    }
    started = true;
    removeLoader(bubble);
    textNode = document.createElement("span");
    textNode.className = "text";
    bubble.insertBefore(textNode, bubble.firstChild);
  };

  const appendText = (text) => {
    beginText();
    // Deltas are accumulated and re-rendered into the same .text span, so the
    // streamed prefixes never duplicate and the markdown stays consistent.
    rawText += text || "";
    window.renderMarkdownInto(textNode, rawText);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  };

  const stageNames = {
    accepted: "Connecting to MCP…",
    mcp_connecting: "Connecting to MCP…",
    model_calling: "Calling the model…",
    tool_calling: "Calling the MCP tool…",
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf("\n\n");
    while (separator !== -1) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      const parsed = parseEventBlock(block);
      if (parsed) {
        const { event, data } = parsed;
        if (event === "delta") {
          appendText(data.text);
        } else if (event === "tool_call") {
          setStage(bubble, `Calling ${data.tool}…`);
          if (data.tool === WEB_SEARCH_TOOL) {
            showSearchSpinner(bubble);
          }
          const args = JSON.stringify(data.arguments || {});
          technicalLine(bubble, `MCP tool call: ${data.tool} ${args}`, false);
        } else if (event === "tool_result") {
          if (data.tool === WEB_SEARCH_TOOL) {
            hideSearchSpinner(bubble);
          }
          technicalLine(
            bubble,
            `${data.tool} ${data.ok ? "returned" : "failed"}: ${data.summary}`,
            !data.ok
          );
        } else if (event === "status") {
          setStage(bubble, stageNames[data.stage] || data.stage);
          technicalLine(bubble, `Status: ${data.stage}`, false);
        } else if (event === "error") {
          failBubble(bubble, describeError(data));
        } else if (event === "done") {
          hideSearchSpinner(bubble);
          removeLoader(bubble);
        }
      }
      separator = buffer.indexOf("\n\n");
    }
  }

  hideSearchSpinner(bubble);
  removeLoader(bubble);
  // A finished request with no text and no technical lines leaves no bubble.
  if (!bubble.querySelector(".text") && !bubble.querySelector("details.technical")) {
    bubble.remove();
  }
}

// -- chats -----------------------------------------------------------------

function readStoredChatId() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeChatId(chatId) {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(chatId));
  } catch {
    return;
  }
}

function clearStoredChatId() {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    return;
  }
}

function showLimit(message) {
  chatLimitMessageEl.textContent = message;
  chatLimitMessageEl.hidden = false;
}

function hideLimit() {
  chatLimitMessageEl.textContent = "";
  chatLimitMessageEl.hidden = true;
}

function setChatActionsEnabled(enabled) {
  newChatButton.disabled = !enabled;
  for (const button of chatsListEl.querySelectorAll("button")) {
    button.disabled = !enabled;
  }
}

async function readDetailMessage(response, fallback) {
  try {
    const payload = await response.json();
    if (payload && payload.detail && payload.detail.message) {
      return payload.detail.message;
    }
  } catch {
    return fallback;
  }
  return fallback;
}

async function createChatRequest() {
  try {
    const response = await fetch(`${API_BASE}/api/chats`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "" }),
    });
    if (response.status === 409) {
      showLimit(await readDetailMessage(response, CHAT_LIMIT_FALLBACK));
      return null;
    }
    if (!response.ok) {
      return null;
    }
    return await response.json();
  } catch {
    return null;
  }
}

function renderChatList() {
  chatsListEl.textContent = "";
  for (const chat of state.chats) {
    const item = document.createElement("li");
    item.className = "chat-item";
    item.dataset.chatId = chat.id;
    if (chat.id === state.activeChatId) {
      item.classList.add("active");
    }

    const title = chat.title || DEFAULT_CHAT_TITLE;
    const select = document.createElement("button");
    select.type = "button";
    select.className = "chat-select";
    select.textContent = title;
    select.addEventListener("click", () => selectChat(chat.id));

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "chat-rename";
    rename.textContent = "Rename";
    rename.setAttribute("aria-label", "Rename chat");
    rename.addEventListener("click", () => renameChat(chat.id));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "chat-delete";
    remove.textContent = "Delete";
    remove.setAttribute("aria-label", "Delete chat");
    remove.addEventListener("click", () => deleteChat(chat.id));

    item.appendChild(select);
    item.appendChild(rename);
    item.appendChild(remove);
    chatsListEl.appendChild(item);
  }
  setChatActionsEnabled(!state.busy);
}

function renderMessages(messages) {
  messagesEl.textContent = "";
  for (const message of messages) {
    if (message.role === "assistant") {
      const bubble = addBubble("assistant", "");
      const body = bubble.querySelector(".text");
      window.renderMarkdownInto(body, message.content || "");
    } else {
      addBubble("user", message.content || "");
    }
  }
}

async function selectChat(chatId) {
  state.activeChatId = chatId;
  storeChatId(chatId);
  renderChatList();
  const token = ++selectionToken;
  try {
    const [messagesResponse, tasksResponse] = await Promise.all([
      fetch(`${API_BASE}/api/chats/${chatId}/messages`),
      fetch(`${API_BASE}/api/chats/${chatId}/tasks`),
    ]);
    if (token !== selectionToken) {
      return;
    }
    messagesEl.textContent = "";
    if (messagesResponse.ok) {
      const payload = await messagesResponse.json();
      renderMessages(payload.messages || []);
    }
    tasksListEl.textContent = "";
    if (tasksResponse.ok) {
      const payload = await tasksResponse.json();
      renderTasks(payload.tasks || []);
    }
  } catch {
    if (token === selectionToken) {
      messagesEl.textContent = "";
      tasksListEl.textContent = "";
    }
  }
}

async function loadChats() {
  let payload;
  try {
    const response = await fetch(`${API_BASE}/api/chats`);
    if (!response.ok) {
      chatsListEl.textContent = "Chats are unavailable.";
      return;
    }
    payload = await response.json();
  } catch {
    chatsListEl.textContent = "Chats are unavailable.";
    return;
  }
  state.chats = payload.chats || [];
  if (!state.chats.length) {
    const created = await createChatRequest();
    if (created) {
      state.chats = [created];
    }
  }
  renderChatList();
  if (!state.chats.length) {
    return;
  }
  const stored = readStoredChatId();
  const preferred =
    state.chats.find((chat) => chat.id === stored) || state.chats[0];
  await selectChat(preferred.id);
}

// Refresh the list (auto title, ordering) without changing the selection.
async function refreshChats() {
  if (!state.activeChatId) {
    return;
  }
  try {
    const response = await fetch(`${API_BASE}/api/chats`);
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    state.chats = payload.chats || [];
    renderChatList();
  } catch {
    return;
  }
}

async function renameChat(chatId) {
  const chat = state.chats.find((item) => item.id === chatId);
  const proposed = window.prompt("Rename chat", chat ? chat.title : "");
  if (proposed === null) {
    return;
  }
  const title = proposed.trim();
  if (!title) {
    return;
  }
  try {
    const response = await fetch(`${API_BASE}/api/chats/${chatId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!response.ok) {
      return;
    }
    const updated = await response.json();
    state.chats = state.chats.map((item) => (item.id === chatId ? updated : item));
    renderChatList();
  } catch {
    return;
  }
}

async function deleteChat(chatId) {
  try {
    let response = await fetch(`${API_BASE}/api/chats/${chatId}`, {
      method: "DELETE",
    });
    if (response.status === 409) {
      const message = await readDetailMessage(
        response,
        "This chat has active scheduled tasks."
      );
      if (!window.confirm(`${message} Delete anyway?`)) {
        return;
      }
      response = await fetch(`${API_BASE}/api/chats/${chatId}?force=true`, {
        method: "DELETE",
      });
    }
    if (!response.ok) {
      return;
    }
    if (state.activeChatId === chatId) {
      state.activeChatId = null;
      clearStoredChatId();
      messagesEl.textContent = "";
      tasksListEl.textContent = "";
    }
    await loadChats();
  } catch {
    return;
  }
}

async function clearChat() {
  if (!state.activeChatId) {
    return;
  }
  const chatId = state.activeChatId;
  try {
    const response = await fetch(`${API_BASE}/api/chats/${chatId}/clear`, {
      method: "POST",
    });
    if (!response.ok) {
      return;
    }
    messagesEl.textContent = "";
    addSystem("This chat was cleared.");
    await refreshChats();
  } catch {
    return;
  }
}

// -- scheduled tasks -------------------------------------------------------

function formatInterval(seconds) {
  const value = Number(seconds);
  if (!isFinite(value) || value <= 0) {
    return "an unknown interval";
  }
  if (value % 86400 === 0) {
    return `${value / 86400} day(s)`;
  }
  if (value % 3600 === 0) {
    return `${value / 3600} hour(s)`;
  }
  if (value % 60 === 0) {
    return `${value / 60} minute(s)`;
  }
  return `${value} seconds`;
}

function formatTimestamp(epoch) {
  if (typeof epoch !== "number" || !isFinite(epoch)) {
    return "unknown time";
  }
  return new Date(epoch * 1000).toLocaleString();
}

function buildTaskLink(item) {
  const entry = document.createElement("li");
  const anchor = document.createElement("a");
  anchor.href = item.url || "#";
  anchor.textContent = item.title || item.url || "";
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  entry.appendChild(anchor);
  return entry;
}

function buildLastRun(run) {
  const block = document.createElement("div");
  block.className = "task-run";
  const line = document.createElement("div");
  line.className = "task-meta";
  line.textContent = `${run.status} at ${formatTimestamp(run.ran_at)}`;
  block.appendChild(line);

  if (run.status === "error") {
    const error = document.createElement("div");
    error.className = "task-error";
    error.textContent = run.error || "The run failed.";
    block.appendChild(error);
    return block;
  }

  const results = Array.isArray(run.results) ? run.results : [];
  const list = document.createElement("ul");
  list.className = "task-links";
  for (const item of results.slice(0, MAX_RENDERED_TASK_LINKS)) {
    list.appendChild(buildTaskLink(item));
  }
  if (list.childElementCount) {
    block.appendChild(list);
  }
  return block;
}

function buildTaskCard(task) {
  const card = document.createElement("div");
  card.className = "task-card";

  const head = document.createElement("div");
  head.className = "task-head";
  const query = document.createElement("span");
  query.className = "task-query";
  query.textContent = task.query || "";
  const status = document.createElement("span");
  status.className = `pill ${task.status === "active" ? "ok" : "warn"}`;
  status.textContent = task.status;
  head.appendChild(query);
  head.appendChild(status);
  card.appendChild(head);

  const interval = document.createElement("div");
  interval.className = "task-meta";
  interval.textContent = `Every ${formatInterval(task.interval_seconds)}`;
  card.appendChild(interval);

  if (task.last_run) {
    card.appendChild(buildLastRun(task.last_run));
  } else {
    const pending = document.createElement("div");
    pending.className = "task-meta";
    pending.textContent = "The first run has not finished yet.";
    card.appendChild(pending);
  }
  return card;
}

function renderTasks(tasks) {
  tasksListEl.textContent = "";
  if (!tasks.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No scheduled tasks in this chat.";
    tasksListEl.appendChild(empty);
    return;
  }
  for (const task of tasks) {
    tasksListEl.appendChild(buildTaskCard(task));
  }
}

async function loadTasks() {
  const chatId = state.activeChatId;
  if (!chatId) {
    tasksListEl.textContent = "";
    return;
  }
  if (tasksRequestInFlight) {
    return;
  }
  tasksRequestInFlight = true;
  const token = selectionToken;
  try {
    const response = await fetch(`${API_BASE}/api/chats/${chatId}/tasks`);
    // A reply for a chat that is no longer selected must not touch the panel.
    if (token !== selectionToken || chatId !== state.activeChatId) {
      return;
    }
    if (!response.ok) {
      tasksListEl.textContent = "";
      return;
    }
    const payload = await response.json();
    renderTasks(payload.tasks || []);
  } catch {
    if (token === selectionToken && chatId === state.activeChatId) {
      tasksListEl.textContent = "";
    }
  } finally {
    tasksRequestInFlight = false;
  }
}

// Idempotent: a second call while the timer already runs is a no-op.
function startTasksPolling() {
  if (tasksPollTimer !== null) {
    return;
  }
  tasksPollTimer = window.setInterval(loadTasks, TASKS_POLL_INTERVAL_MS);
}

function stopTasksPolling() {
  if (tasksPollTimer === null) {
    return;
  }
  window.clearInterval(tasksPollTimer);
  tasksPollTimer = null;
}

// -- send ------------------------------------------------------------------

async function send() {
  if (state.busy) {
    return;
  }
  const message = inputEl.value.trim();
  if (!message) {
    return;
  }
  state.busy = true;
  setChatActionsEnabled(false);
  sendButton.disabled = true;
  addBubble("user", message);
  // The loader appears synchronously, before the request is sent.
  const bubble = addLoader();
  inputEl.value = "";
  try {
    await sendMessage(message, bubble);
  } catch (error) {
    failBubble(bubble, `The request failed: ${error.message || error}`);
  } finally {
    state.busy = false;
    setChatActionsEnabled(true);
    sendButton.disabled = false;
    inputEl.focus();
  }
  await refreshChats();
}

function pill(ok, label) {
  const span = document.createElement("span");
  span.className = `pill ${ok ? "ok" : "bad"}`;
  span.textContent = label;
  return span;
}

function statusRow(label, valueNode) {
  const row = document.createElement("div");
  row.className = "status-row";
  const name = document.createElement("span");
  name.className = "status-label";
  name.textContent = label;
  row.appendChild(name);
  row.appendChild(valueNode);
  return row;
}

function textNode(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

function renderTools(container, tools) {
  if (!tools.length) {
    container.appendChild(textNode("No tools were advertised."));
    return;
  }
  for (const tool of tools) {
    const card = document.createElement("div");
    card.className = "tool-card";
    const name = document.createElement("div");
    name.className = "tool-name";
    name.textContent = tool.name;
    card.appendChild(name);
    const description = document.createElement("div");
    description.className = "tool-desc";
    description.textContent = tool.description || "(no description)";
    card.appendChild(description);
    if (tool.input_schema && Object.keys(tool.input_schema).length) {
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(tool.input_schema, null, 2);
      card.appendChild(pre);
    }
    container.appendChild(card);
  }
}

async function refreshStatus() {
  mcpStatusEl.textContent = "";
  mcpStatusEl.appendChild(
    Object.assign(document.createElement("p"), {
      className: "muted",
      textContent: "Checking the MCP server…",
    })
  );
  let status = null;
  let tools = null;
  try {
    const [statusResponse, toolsResponse] = await Promise.all([
      fetch(`${API_BASE}/api/mcp/status`),
      fetch(`${API_BASE}/api/mcp/tools`),
    ]);
    status = statusResponse.ok ? await statusResponse.json() : null;
    tools = toolsResponse.ok ? await toolsResponse.json() : null;
  } catch (error) {
    mcpStatusEl.textContent = "";
    mcpStatusEl.appendChild(
      textNode(`The backend did not answer: ${error.message || error}`)
    );
    return;
  }

  mcpStatusEl.textContent = "";
  if (!status) {
    mcpStatusEl.appendChild(
      textNode("The backend did not answer the MCP status request.")
    );
    return;
  }

  mcpStatusEl.appendChild(
    statusRow(
      "Connection",
      pill(status.connected, status.connected ? "connected" : "disconnected")
    )
  );
  if (status.protocol_version) {
    mcpStatusEl.appendChild(
      statusRow("Protocol version", textNode(status.protocol_version))
    );
  }
  if (status.server) {
    mcpStatusEl.appendChild(
      statusRow(
        "Server",
        textNode(
          `${status.server.name || "unknown"} ${status.server.version || ""}`.trim()
        )
      )
    );
  }
  mcpStatusEl.appendChild(
    statusRow("Tools count", textNode(String(status.tools_count ?? 0)))
  );
  if (status.error) {
    mcpStatusEl.appendChild(
      statusRow("Error", textNode(`${status.error.category}: ${status.error.message}`))
    );
  }

  const list = tools && tools.tools ? tools.tools : [];
  const details = document.createElement("details");
  details.className = "tools";
  const summary = document.createElement("summary");
  summary.textContent = `Tools (${list.length})`;
  details.appendChild(summary);
  const container = document.createElement("div");
  renderTools(container, list);
  details.appendChild(container);
  mcpStatusEl.appendChild(details);
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  send();
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

clearButton.addEventListener("click", clearChat);

newChatButton.addEventListener("click", async () => {
  if (state.busy) {
    return;
  }
  hideLimit();
  const created = await createChatRequest();
  if (!created) {
    return;
  }
  state.chats = [...state.chats, created];
  renderChatList();
  await selectChat(created.id);
});

tasksRefreshButton.addEventListener("click", loadTasks);

refreshButton.addEventListener("click", refreshStatus);

// The timer must not survive a navigation away from the page.
window.addEventListener("pagehide", stopTasksPolling);
window.addEventListener("beforeunload", stopTasksPolling);
window.addEventListener("pageshow", startTasksPolling);

loadChats();
refreshStatus();
startTasksPolling();
inputEl.focus();

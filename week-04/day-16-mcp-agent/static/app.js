"use strict";

// Relative base: the UI and the API share one origin. It stays empty so the
// same build works behind a reverse proxy later, and it never carries a
// credential, an Authorization header or an upstream provider key.
const API_BASE = "";

const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const sendButton = document.getElementById("send-button");
const clearButton = document.getElementById("clear-button");
const refreshButton = document.getElementById("refresh-button");
const mcpStatusEl = document.getElementById("mcp-status");

const initialSessionId =
  typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `session-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

let sessionId = initialSessionId;
let busy = false;

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
    body: JSON.stringify({ session_id: sessionId, message }),
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
          const args = JSON.stringify(data.arguments || {});
          technicalLine(bubble, `MCP tool call: ${data.tool} ${args}`, false);
        } else if (event === "tool_result") {
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
          removeLoader(bubble);
        }
      }
      separator = buffer.indexOf("\n\n");
    }
  }

  removeLoader(bubble);
  // A finished request with no text and no technical lines leaves no bubble.
  if (!bubble.querySelector(".text") && !bubble.querySelector("details.technical")) {
    bubble.remove();
  }
}

async function send() {
  if (busy) {
    return;
  }
  const message = inputEl.value.trim();
  if (!message) {
    return;
  }
  busy = true;
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
    busy = false;
    sendButton.disabled = false;
    inputEl.focus();
  }
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

clearButton.addEventListener("click", () => {
  messagesEl.textContent = "";
  sessionId =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `session-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  addSystem("New session. The previous history is no longer used.");
});

refreshButton.addEventListener("click", refreshStatus);

refreshStatus();
inputEl.focus();

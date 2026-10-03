// Shared helpers for the shuttle pages.

export async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function post(path, body = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "입력값을 다시 확인해 주세요.";
    throw new Error(detail);
  }
  return data;
}

// Live state: server-sent events, falling back to polling when the stream drops.
export function connect(onState) {
  let pollTimer = null;
  let source = null;

  const poll = () => getJSON("/api/state").then(onState).catch(() => {});

  const startPolling = () => {
    if (pollTimer) return;
    pollTimer = setInterval(poll, 2000);
  };
  const stopPolling = () => {
    clearInterval(pollTimer);
    pollTimer = null;
  };

  const open = () => {
    if (!("EventSource" in window)) {
      startPolling();
      return;
    }
    source = new EventSource("/api/stream");
    source.onmessage = (ev) => {
      stopPolling();
      onState(JSON.parse(ev.data));
    };
    source.onerror = () => {
      source.close();
      startPolling();
      setTimeout(open, 10000);
    };
  };

  poll();
  open();
}

export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function modeText(state) {
  if (state.mode === "real") return state.manual ? "실제 운행" : "실제 시각 · 자동 운행";
  return `시연 · ${state.speed}배속`;
}

export function renderTop(state) {
  const clock = document.getElementById("clock");
  const mode = document.getElementById("mode");
  if (clock) clock.textContent = state.clock;
  if (mode) {
    mode.textContent = modeText(state);
    mode.classList.toggle("demo", state.mode === "demo");
  }
}

// One line about where the shuttle is, for teachers and the board.
export function vehicleLine(state) {
  const v = state.vehicle;
  switch (v.status) {
    case "driving":
      return `${v.stop_name} 가는 중 · ${v.until_text} 도착 예정`;
    case "waiting":
      return `${v.stop_name}에서 대기 중`;
    case "dwell":
    case "free":
      return `${v.stop_name}에서 타고 내리는 중`;
    case "ready":
      return `${v.stop_name}에서 출발 준비 중`;
    default:
      return `${v.stop_name}에서 대기 중`;
  }
}

// "① 마을" shown next to a big "①" reads as "마을".
export function plainName(name, short) {
  return name.startsWith(short) ? name.slice(short.length).trim() || name : name;
}

export function minutes(value) {
  if (value === null || value === undefined) return "–";
  return `${Math.round(value)}`;
}

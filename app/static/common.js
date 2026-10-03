// Shared helpers for the shuttle pages.

function toLogin() {
  const next = location.pathname + location.search;
  location.href = `/login?next=${encodeURIComponent(next)}`;
}

// If the academy uses an access code and this device has not entered it yet, go to the code page.
export async function ensureSession() {
  try {
    const res = await fetch("/api/session", { cache: "no-store" });
    const s = await res.json();
    if (s.required && !s.ok) toLogin();
    return s;
  } catch {
    return { required: false, ok: true };
  }
}

export async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (res.status === 401) {
    toLogin();
    throw new Error("학원 코드를 먼저 입력해 주세요.");
  }
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
  if (res.status === 401 && path !== "/api/login") {
    toLogin();
    throw new Error("학원 코드를 먼저 입력해 주세요.");
  }
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
    case "boarding":
      return `${v.stop_name}에서 학생을 태우는 중`;
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

// The role this device was last used for, so the start page can offer it first.
export const role = {
  get() {
    try {
      return JSON.parse(localStorage.getItem("shuttle-role") || "null");
    } catch {
      return null;
    }
  },
  set(value) {
    try {
      localStorage.setItem("shuttle-role", JSON.stringify(value));
    } catch {}
  },
};

// Attention signal: vibration where the phone supports it, plus a short chime once a tap has unlocked audio.
let audio = null;
export function unlockSound() {
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    audio.resume?.();
  } catch {}
}
export function signal() {
  try {
    navigator.vibrate?.([220, 120, 220]);
  } catch {}
  if (!audio) return;
  try {
    const t0 = audio.currentTime;
    [880, 1175].forEach((freq, i) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      const t = t0 + i * 0.24;
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      osc.connect(gain).connect(audio.destination);
      osc.start(t);
      osc.stop(t + 0.23);
    });
  } catch {}
}

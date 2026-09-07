"use strict";

const chat = document.getElementById("chat");
const banner = document.getElementById("banner");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");

const SESSION_KEY = "study-planner-session";
let sessionId = null;
let lastStep = null;
let pollTimer = null;

function addMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

function addGuide(markdown) {
  const el = document.createElement("div");
  el.className = "msg bot guide";
  el.innerHTML = window.marked ? window.marked.parse(markdown) : `<pre>${markdown}</pre>`;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function setBanner(kind, text) {
  banner.hidden = false;
  banner.className = `banner ${kind}`;
  banner.textContent = text;
}

function setComposerEnabled(on) {
  input.disabled = !on;
  sendBtn.disabled = !on;
  input.value = "";
  if (on) input.focus();
}

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

function render(state, opts = {}) {
  const step = state.step;

  if (["ASK_CERTS", "ASK_BACKGROUND", "ASK_GOAL"].includes(step)) {
    if (step !== lastStep && state.prompt) addMessage("bot", state.prompt);
    setComposerEnabled(true);
  } else {
    setComposerEnabled(false);
  }

  if (step === "GENERATING") {
    addMessage("bot typing", "Building your study guide from Microsoft Learn…");
  }

  if (state.guide && (opts.forceGuide || step !== lastStep)) {
    if (!document.querySelector(".msg.guide")) {
      addMessage("bot", "Here is your draft study plan:");
      addGuide(state.guide.markdown);
    }
  }

  if (state.guide) {
    const g = state.guide;
    if (g.status === "PendingApproval") {
      setBanner("pending", `Waiting for approval from ${g.approver_email}…`);
      startPolling(g.guide_id);
    } else if (g.status === "Approved") {
      stopPolling();
      const note = g.approver_comment ? ` — “${g.approver_comment}”` : "";
      setBanner("approved", `Plan approved by ${g.approver_email} ✅${note}. You're clear to start!`);
    } else if (g.status === "Rejected") {
      stopPolling();
      const note = g.approver_comment ? ` — “${g.approver_comment}”` : "";
      setBanner("rejected", `Plan rejected by ${g.approver_email}${note}. Please revise and resubmit.`);
    }
  }

  lastStep = step;
}

function startPolling(guideId) {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const g = await api(`/api/guides/${guideId}`);
      if (g.status !== "PendingApproval") {
        stopPolling();
        render({ step: "PENDING_APPROVAL", prompt: null, answers: {}, guide: g }, { forceGuide: false });
      }
    } catch (err) {
      console.warn("poll failed", err);
    }
  }, 3000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || input.disabled) return;
  addMessage("user", text);
  input.value = "";
  setComposerEnabled(false);
  try {
    const state = await api("/api/message", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    render(state);
  } catch (err) {
    addMessage("bot", `⚠️ ${err.message}`);
    setComposerEnabled(true);
  }
});

(async function init() {
  try {
    sessionId = localStorage.getItem(SESSION_KEY);
  } catch (_) {
    sessionId = null;
  }
  try {
    const state = await api("/api/session", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });
    sessionId = state.session_id;
    try {
      localStorage.setItem(SESSION_KEY, sessionId);
    } catch (_) {
      /* ignore */
    }
    addMessage("bot", "Hi! I'll build you a Microsoft certification study plan. A few quick questions first.");
    render(state, { forceGuide: true });
  } catch (err) {
    addMessage("bot", `⚠️ Could not start a session: ${err.message}`);
  }
})();

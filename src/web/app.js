"use strict";

const chat = document.getElementById("chat");
const banner = document.getElementById("banner");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");

const SESSION_KEY = "study-planner-session";
const INTAKE_STEPS = ["ASK_CERTS", "ASK_BACKGROUND", "ASK_GOAL"];

let sessionId = null;
let lastStep = null;
let pollTimer = null;
let typingEl = null;

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

function showTyping(text) {
  clearTyping();
  typingEl = addMessage("bot typing", text);
}

function clearTyping() {
  if (typingEl) {
    typingEl.remove();
    typingEl = null;
  }
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

function renderHistory(history) {
  for (const turn of history || []) addMessage(turn.role, turn.text);
}

function render(state, opts = {}) {
  const step = state.step;
  clearTyping();

  if (INTAKE_STEPS.includes(step)) {
    if (step !== lastStep && state.prompt) addMessage("bot", state.prompt);
    setComposerEnabled(true);
  } else {
    setComposerEnabled(false);
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
        render({ step: "PENDING_APPROVAL", prompt: null, guide: g });
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
  const finishing = lastStep === "ASK_GOAL";
  if (finishing) showTyping("Building your study guide from Microsoft Learn — this can take a moment…");

  try {
    const state = await api("/api/message", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    render(state);
  } catch (err) {
    clearTyping();
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

  let state;
  try {
    state = await api("/api/session", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId }),
    });

    // Don't silently resume a half-finished intake (e.g. left over from an
    // earlier failed attempt) — start clean. Only resume a fresh session or one
    // that already has a guide to track.
    const resumable = state.step === "ASK_CERTS" || state.guide;
    if (!resumable) {
      try {
        localStorage.removeItem(SESSION_KEY);
      } catch (_) {
        /* ignore */
      }
      state = await api("/api/session", {
        method: "POST",
        body: JSON.stringify({ session_id: null }),
      });
    }

    sessionId = state.session_id;
    try {
      localStorage.setItem(SESSION_KEY, sessionId);
    } catch (_) {
      /* ignore */
    }

    addMessage("bot", "Hi! I'll build you a Microsoft certification study plan. A few quick questions first.");
    renderHistory(state.history);
    render(state, { forceGuide: true });
  } catch (err) {
    addMessage("bot", `⚠️ Could not start a session: ${err.message}`);
  }
})();

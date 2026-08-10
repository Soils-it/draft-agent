(() => {
  "use strict";

  const generation = Number(window.__ESPN_DRAFT_AGENT_CONTENT_GENERATION__ || 0) + 1;
  window.__ESPN_DRAFT_AGENT_CONTENT_GENERATION__ = generation;
  const isCurrent = () => window.__ESPN_DRAFT_AGENT_CONTENT_GENERATION__ === generation;
  const PAGE_SOURCE = "ESPN_DRAFT_AGENT_PAGE";
  const CONTENT_SOURCE = "ESPN_DRAFT_AGENT_CONTENT";

  async function sendRuntime(message) {
    if (!isCurrent() || !chrome.runtime?.id) return;
    try {
      await chrome.runtime.sendMessage(message);
    } catch (error) {
      if (!String(error).includes("Extension context invalidated")) throw error;
    }
  }

  window.addEventListener("message", (event) => {
    if (!isCurrent() || event.source !== window || event.data?.source !== PAGE_SOURCE) return;
    if (event.data.type === "SNAPSHOT") {
      const snapshot = event.data.snapshot;
      if (snapshot && Array.isArray(snapshot.available_player_ids)) {
        void sendRuntime({ type: "DRAFT_AGENT_SNAPSHOT", snapshot }).catch(() => {});
      }
      return;
    }
    if (event.data.type === "MOCK_PICK_RESULT") {
      void sendRuntime({
        type: "DRAFT_AGENT_PICK_RESULT",
        result: event.data.result
      }).catch(() => {});
    }
  });

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (!isCurrent()) return false;
    if (message?.type === "DRAFT_AGENT_SYNC") {
      window.postMessage({ source: CONTENT_SOURCE, type: "REQUEST_SNAPSHOT" }, "*");
      respond({ ok: true });
      return false;
    }
    if (message?.type === "DRAFT_AGENT_MOCK_PICK") {
      window.postMessage({
        source: CONTENT_SOURCE,
        type: "MOCK_PICK",
        command: message.command
      }, "*");
      respond({ ok: true });
      return false;
    }
    return false;
  });

  window.postMessage({ source: CONTENT_SOURCE, type: "REQUEST_SNAPSHOT" }, "*");
})();

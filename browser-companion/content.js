(() => {
  "use strict";

  if (window.__ESPN_DRAFT_AGENT_CONTENT_ACTIVE__) return;
  window.__ESPN_DRAFT_AGENT_CONTENT_ACTIVE__ = true;

  const LOCAL_ENDPOINT = "http://127.0.0.1:8765/api/espn/snapshot";
  const PAGE_SOURCE = "ESPN_DRAFT_AGENT_PAGE";
  const CONTENT_SOURCE = "ESPN_DRAFT_AGENT_CONTENT";
  const attemptedPicks = new Set();
  let pendingTimer = null;
  let pendingPickKey = null;

  async function saveStatus(status) {
    await chrome.storage.local.set({
      draftAgentLastStatus: { ...status, at: new Date().toISOString() }
    });
  }

  async function sync(snapshot) {
    const settings = await chrome.storage.local.get({ enabled: false, autoPickMocks: false });
    if (!settings.enabled) return;
    try {
      const response = await fetch(LOCAL_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Local bridge rejected the snapshot");
      scheduleMockPick(snapshot, result, settings.autoPickMocks);
      await saveStatus({
        ok: true,
        message: settings.autoPickMocks
          ? "Projection snapshot accepted; mock auto-pick is armed."
          : "Projection snapshot accepted; mock auto-pick is off.",
        overallPick: snapshot.overall_pick,
        matchRate: result.espn.match_rate
      });
    } catch (error) {
      await saveStatus({ ok: false, message: String(error.message || error) });
    }
  }

  function scheduleMockPick(snapshot, result, enabled) {
    const recommendation = result?.espn?.pending_espn_player_id;
    const pickKey = `${snapshot.league_id}:${snapshot.overall_pick}`;
    if (!enabled || !snapshot.on_clock || !recommendation || attemptedPicks.has(pickKey)) {
      if (pendingTimer) clearTimeout(pendingTimer);
      pendingTimer = null;
      pendingPickKey = null;
      return;
    }
    if (pendingPickKey === pickKey) return;
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingPickKey = pickKey;
    const delay = Math.max(5, Number(result?.settings?.override_seconds) || 20) * 1000;
    pendingTimer = setTimeout(() => {
      attemptedPicks.add(pickKey);
      pendingTimer = null;
      pendingPickKey = null;
      window.postMessage({
        source: CONTENT_SOURCE,
        type: "MOCK_PICK",
        command: {
          mock_only: true,
          league_id: snapshot.league_id,
          overall_pick: snapshot.overall_pick,
          player_id: recommendation
        }
      }, "*");
    }, delay);
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.data?.source !== PAGE_SOURCE) return;
    if (event.data.type === "SNAPSHOT") {
      const snapshot = event.data.snapshot;
      if (snapshot && Array.isArray(snapshot.available_player_ids)) sync(snapshot);
      return;
    }
    if (event.data.type !== "MOCK_PICK_RESULT") return;
    const result = event.data.result;
    if (!result) return;
    saveStatus({ ok: result.ok === true, message: String(result.message || "Mock selection finished.") });
  });

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type !== "DRAFT_AGENT_SYNC") return false;
    window.postMessage({ source: CONTENT_SOURCE, type: "REQUEST_SNAPSHOT" }, "*");
    respond({ ok: true });
    return false;
  });

  window.postMessage({ source: CONTENT_SOURCE, type: "REQUEST_SNAPSHOT" }, "*");
})();

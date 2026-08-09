(() => {
  "use strict";

  const LOCAL_ENDPOINT = "http://127.0.0.1:8765/api/espn/snapshot";
  const SNAPSHOT_EVENT = "ESPN_DRAFT_AGENT_SNAPSHOT";
  const REQUEST_EVENT = "ESPN_DRAFT_AGENT_REQUEST";

  async function saveStatus(status) {
    await chrome.storage.local.set({
      draftAgentLastStatus: { ...status, at: new Date().toISOString() }
    });
  }

  async function sync(snapshot) {
    const settings = await chrome.storage.local.get({ enabled: false });
    if (!settings.enabled) return;
    try {
      const response = await fetch(LOCAL_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Local bridge rejected the snapshot");
      await saveStatus({
        ok: true,
        message: "Shadow snapshot accepted; real submission remains disabled.",
        overallPick: snapshot.overall_pick,
        matchRate: result.espn.match_rate
      });
    } catch (error) {
      await saveStatus({ ok: false, message: String(error.message || error) });
    }
  }

  window.addEventListener(SNAPSHOT_EVENT, (event) => {
    const snapshot = event.detail;
    if (!snapshot || !Array.isArray(snapshot.available_player_ids)) return;
    sync(snapshot);
  });

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type !== "DRAFT_AGENT_SYNC") return false;
    window.dispatchEvent(new CustomEvent(REQUEST_EVENT));
    respond({ ok: true });
    return false;
  });

  window.dispatchEvent(new CustomEvent(REQUEST_EVENT));
})();

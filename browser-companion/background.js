"use strict";

const LOCAL_ENDPOINT = "http://127.0.0.1:8765/api/espn/snapshot";
const activePicks = new Map();

async function saveStatus(status) {
  await chrome.storage.local.set({
    draftAgentLastStatus: { ...status, at: new Date().toISOString() }
  });
}

async function handleSnapshot(snapshot, tabId) {
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
    await saveStatus({
      ok: true,
      message: settings.autoPickMocks
        ? "Projection snapshot accepted; mock auto-pick is armed."
        : "Projection snapshot accepted; mock auto-pick is off.",
      overallPick: snapshot.overall_pick,
      matchRate: result.espn.match_rate
    });
    await considerMockPick(snapshot, result, settings.autoPickMocks, tabId);
  } catch (error) {
    await saveStatus({ ok: false, message: String(error?.message || error) });
  }
}

async function considerMockPick(snapshot, result, enabled, tabId) {
  const recommendation = result?.espn?.pending_espn_player_id;
  const pickKey = `${snapshot.league_id}:${snapshot.overall_pick}`;
  const current = activePicks.get(tabId);
  if (!enabled || !snapshot.on_clock || !recommendation) {
    activePicks.delete(tabId);
    return;
  }
  if (!current || current.key !== pickKey) {
    activePicks.set(tabId, { key: pickKey, startedAt: Date.now(), attempted: false });
    return;
  }
  const delay = Math.max(5, Number(result?.settings?.override_seconds) || 20) * 1000;
  if (current.attempted || Date.now() - current.startedAt < delay) return;
  current.attempted = true;
  await chrome.tabs.sendMessage(tabId, {
    type: "DRAFT_AGENT_MOCK_PICK",
    command: {
      mock_only: true,
      league_id: snapshot.league_id,
      overall_pick: snapshot.overall_pick,
      player_id: recommendation
    }
  });
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type === "DRAFT_AGENT_SNAPSHOT" && sender.tab?.id) {
    handleSnapshot(message.snapshot, sender.tab.id)
      .then(() => respond({ ok: true }))
      .catch((error) => respond({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "DRAFT_AGENT_PICK_RESULT") {
    const result = message.result;
    void saveStatus({
      ok: result?.ok === true,
      message: String(result?.message || "Mock selection finished.")
    });
  }
  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => activePicks.delete(tabId));

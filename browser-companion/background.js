"use strict";

const LOCAL_ENDPOINT = "http://127.0.0.1:8765/api/espn/snapshot";
const PICK_RESULT_ENDPOINT = "http://127.0.0.1:8765/api/espn/pick-result";
const ESPN_DRAFT_URL = "https://fantasy.espn.com/football/draft*";
const activePicks = new Map();
const snapshotQueues = new Map();

async function saveStatus(status) {
  await chrome.storage.local.set({
    draftAgentLastStatus: { ...status, at: new Date().toISOString() }
  });
}

async function injectCompanion(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["page-observer.js"],
      world: "MAIN"
    });
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"],
      world: "ISOLATED"
    });
    await chrome.tabs.sendMessage(tabId, { type: "DRAFT_AGENT_SYNC" });
  } catch (error) {
    await saveStatus({
      ok: false,
      message: `Automatic draft connection failed: ${String(error?.message || error)}`
    });
  }
}

async function injectOpenDrafts() {
  const tabs = await chrome.tabs.query({ url: [ESPN_DRAFT_URL] });
  await Promise.all(tabs.filter((tab) => tab.id).map((tab) => injectCompanion(tab.id)));
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
    const ready = result?.espn?.readiness?.ready === true;
    await saveStatus({
      ok: ready,
      message: ready
        ? (settings.autoPickMocks
          ? "READY: snapshot accepted; mock auto-pick is armed."
          : "READY: snapshot accepted; mock auto-pick is off.")
        : `NOT READY: ${result?.espn?.readiness?.reasons?.[0] || "local data checks failed."}`,
      overallPick: snapshot.overall_pick,
      matchRate: result.espn.match_rate
    });
    await considerMockPick(snapshot, result, settings.autoPickMocks, tabId);
  } catch (error) {
    await saveStatus({ ok: false, message: String(error?.message || error) });
  }
}

async function queueSnapshot(snapshot, tabId) {
  const current = snapshotQueues.get(tabId);
  if (current) {
    current.pending = snapshot;
    return;
  }
  const queue = { pending: null };
  snapshotQueues.set(tabId, queue);
  try {
    let next = snapshot;
    while (next) {
      await handleSnapshot(next, tabId);
      next = queue.pending;
      queue.pending = null;
    }
  } finally {
    snapshotQueues.delete(tabId);
  }
}

async function considerMockPick(snapshot, result, enabled, tabId) {
  const recommendation = result?.espn?.pending_espn_player_id;
  const pickKey = `${snapshot.league_id}:${snapshot.overall_pick}`;
  const current = activePicks.get(tabId);
  if (!enabled || result?.espn?.on_clock !== true) {
    activePicks.delete(tabId);
    return;
  }
  if (result?.espn?.readiness?.ready !== true || !recommendation) return;
  if (!current || current.key !== pickKey) {
    activePicks.set(tabId, {
      key: pickKey,
      startedAt: Date.now(),
      attempted: false,
      recoveryCount: 0,
      awaitingFreshSnapshot: false,
      recommendation
    });
    return;
  }
  current.recommendation = recommendation;
  if (current.awaitingFreshSnapshot) {
    current.awaitingFreshSnapshot = false;
    current.attempted = false;
    current.startedAt = Date.now();
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

function isRecoverablePickFailure(result) {
  const message = String(result?.message || "").toLowerCase();
  return message.includes("stale") || message.includes("unavailable");
}

async function handlePickResult(result, tabId) {
  if (result?.ok === true) {
    await recordPickResult(result);
    await saveStatus({ ok: true, message: String(result.message || "Mock selection finished.") });
    return;
  }
  const current = tabId ? activePicks.get(tabId) : null;
  const resultKey = `${result?.league_id || result?.draft_id || ""}:${result?.overall_pick || ""}`;
  if (
    current &&
    current.key === resultKey &&
    current.attempted &&
    current.recoveryCount < 1 &&
    isRecoverablePickFailure(result)
  ) {
    current.recoveryCount += 1;
    current.awaitingFreshSnapshot = true;
    await saveStatus({
      ok: false,
      message: `${String(result.message || "Mock selection was rejected")} Requesting one fresh snapshot.`
    });
    await chrome.tabs.sendMessage(tabId, { type: "DRAFT_AGENT_SYNC" });
    return;
  }
  await saveStatus({ ok: false, message: String(result?.message || "Mock selection failed.") });
}

async function recordPickResult(result) {
  if (result?.ok !== true) return;
  const response = await fetch(PICK_RESULT_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(result)
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Local decision log rejected the pick result");
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type === "DRAFT_AGENT_SNAPSHOT" && sender.tab?.id) {
    queueSnapshot(message.snapshot, sender.tab.id)
      .then(() => respond({ ok: true }))
      .catch((error) => respond({ ok: false, error: String(error?.message || error) }));
    return true;
  }
  if (message?.type === "DRAFT_AGENT_PICK_RESULT") {
    void handlePickResult(message.result, sender.tab?.id).catch((error) => saveStatus({
      ok: false,
      message: `Mock pick result handling failed: ${String(error?.message || error)}`
    }));
  }
  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  activePicks.delete(tabId);
  snapshotQueues.delete(tabId);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url?.startsWith("https://fantasy.espn.com/football/draft")) {
    void injectCompanion(tabId);
  }
});
chrome.runtime.onInstalled.addListener(() => void injectOpenDrafts());
chrome.runtime.onStartup.addListener(() => void injectOpenDrafts());

"use strict";

const fields = [
  "overallPickSelector",
  "onClockSelector",
  "availablePlayerSelector",
  "rosterPlayerSelector",
  "playerIdAttribute"
];

async function render() {
  const settings = await chrome.storage.local.get({ enabled: false, playerIdAttribute: "data-player-id" });
  document.querySelector("#enabled").checked = settings.enabled;
  for (const field of fields) document.querySelector(`#${field}`).value = settings[field] || "";
  const status = settings.draftAgentLastStatus;
  document.querySelector("#status").textContent = status
    ? `${status.ok ? "Connected" : "Not connected"}: ${status.message}\n${status.at}`
    : "No snapshot sent yet.";
}

document.querySelector("#save").addEventListener("click", async () => {
  const settings = { enabled: document.querySelector("#enabled").checked };
  for (const field of fields) settings[field] = document.querySelector(`#${field}`).value.trim();
  await chrome.storage.local.set(settings);
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "DRAFT_AGENT_SYNC" });
    } catch (_error) {
      await chrome.storage.local.set({
        draftAgentLastStatus: {
          ok: false,
          message: "Open an ESPN fantasy draft page before syncing.",
          at: new Date().toISOString()
        }
      });
    }
  }
  await render();
});

render();

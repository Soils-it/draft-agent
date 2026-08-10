"use strict";

async function render() {
  const settings = await chrome.storage.local.get({ enabled: false, autoPickMocks: false });
  document.querySelector("#enabled").checked = settings.enabled;
  document.querySelector("#autoPickMocks").checked = settings.autoPickMocks;
  const status = settings.draftAgentLastStatus;
  document.querySelector("#status").textContent = status
    ? `${status.ok ? "Connected" : "Not connected"}: ${status.message}\n${status.at}`
    : "No snapshot sent yet.";
}

document.querySelector("#save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    enabled: document.querySelector("#enabled").checked,
    autoPickMocks: document.querySelector("#autoPickMocks").checked
  });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["page-observer.js"],
        world: "MAIN"
      });
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"],
        world: "ISOLATED"
      });
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

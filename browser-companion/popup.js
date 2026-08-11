"use strict";

async function render() {
  const settings = await chrome.storage.local.get({ enabled: false, autoPickMocks: false });
  document.querySelector("#enabled").checked = settings.enabled;
  document.querySelector("#autoPickMocks").checked = settings.autoPickMocks;
  const status = settings.draftAgentLastStatus;
  const statusLabel = status?.message?.startsWith("NOT READY")
    ? "Not ready"
    : (status?.ok ? "Connected" : "Not connected");
  document.querySelector("#status").textContent = status
    ? `${statusLabel}: ${status.message}\n${status.at}`
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
    } catch (error) {
      await chrome.storage.local.set({
        draftAgentLastStatus: {
          ok: false,
          message: String(error?.message || error || "Companion injection failed."),
          at: new Date().toISOString()
        }
      });
    }
  }
  await render();
});

render();

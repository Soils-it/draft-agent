"use strict";

const fs = require("fs");
const vm = require("vm");

let saveHandler = null;
const injections = [];
let sentMessage = null;
const stored = {
  enabled: true,
  autoPickMocks: true,
  draftAgentLastStatus: null
};
const elements = {
  enabled: { checked: true },
  autoPickMocks: { checked: true },
  status: { textContent: "" },
  save: { addEventListener: (_type, handler) => { saveHandler = handler; } }
};
const context = {
  console,
  document: { querySelector: (selector) => elements[selector.slice(1)] },
  chrome: {
    storage: {
      local: {
        get: async (defaults) => ({ ...defaults, ...stored }),
        set: async (values) => Object.assign(stored, values)
      }
    },
    tabs: {
      query: async () => [{ id: 17 }],
      sendMessage: async (_id, message) => { sentMessage = message; }
    },
    scripting: {
      executeScript: async (details) => { injections.push(details); }
    }
  }
};

vm.runInNewContext(fs.readFileSync("browser-companion/popup.js", "utf8"), context);

(async () => {
  if (!saveHandler) throw new Error("Save handler was not registered");
  await saveHandler();
  if (injections.length !== 2) throw new Error("Expected exactly two script injections");
  if (injections[0].world !== "MAIN" || injections[0].files[0] !== "page-observer.js") {
    throw new Error("Page observer was not injected into the main world first");
  }
  if (injections[1].world !== "ISOLATED" || injections[1].files[0] !== "content.js") {
    throw new Error("Content bridge was not injected into the isolated world second");
  }
  if (sentMessage?.type !== "DRAFT_AGENT_SYNC") {
    throw new Error("Sync request was not sent after injection");
  }
  console.log("popup injection tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

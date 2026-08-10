"use strict";

const fs = require("fs");
const vm = require("vm");

const listeners = new Map();
const runtimeMessages = [];
const window = {
  addEventListener(type, callback) {
    listeners.set(type, [...(listeners.get(type) || []), callback]);
  },
  postMessage(data) {
    for (const callback of listeners.get("message") || []) {
      callback({ source: window, data });
    }
  }
};
const context = {
  window,
  console,
  chrome: {
    runtime: {
      id: "test-extension",
      sendMessage: async (message) => { runtimeMessages.push(message); },
      onMessage: { addListener() {} }
    }
  }
};
const source = fs.readFileSync("browser-companion/content.js", "utf8");
vm.runInNewContext(source, context);
vm.runInNewContext(source, context);

window.postMessage({
  source: "ESPN_DRAFT_AGENT_PAGE",
  type: "SNAPSHOT",
  snapshot: { overall_pick: 1, available_player_ids: ["42"] }
});

setTimeout(() => {
  if (runtimeMessages.length !== 1 || runtimeMessages[0].type !== "DRAFT_AGENT_SNAPSHOT") {
    throw new Error("Only the latest content generation should relay one snapshot");
  }
  console.log("content generation tests passed");
}, 20);

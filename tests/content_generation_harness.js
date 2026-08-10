"use strict";

const fs = require("fs");
const vm = require("vm");

const listeners = new Map();
let fetchCalls = 0;
let storageReads = 0;
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
  setTimeout,
  clearTimeout,
  fetch: async () => {
    fetchCalls += 1;
    return {
      ok: true,
      json: async () => ({
        espn: { pending_espn_player_id: null, match_rate: 1 },
        settings: { override_seconds: 20 }
      })
    };
  },
  chrome: {
    runtime: {
      id: "test-extension",
      onMessage: { addListener() {} }
    },
    storage: {
      local: {
        get: async () => {
          storageReads += 1;
          return { enabled: true, autoPickMocks: false };
        },
        set: async () => {}
      }
    }
  }
};
const source = fs.readFileSync("browser-companion/content.js", "utf8");
vm.runInNewContext(source, context);
vm.runInNewContext(source, context);

window.postMessage({
  source: "ESPN_DRAFT_AGENT_PAGE",
  type: "SNAPSHOT",
  snapshot: {
    league_id: "MOCK",
    overall_pick: 1,
    on_clock: false,
    available_player_ids: ["42"]
  }
});

setTimeout(() => {
  if (fetchCalls !== 1 || storageReads !== 1) {
    throw new Error(`Only the latest content generation should sync (fetch=${fetchCalls}, storage=${storageReads})`);
  }
  console.log("content generation tests passed");
}, 20);

"use strict";

const fs = require("fs");
const vm = require("vm");

let runtimeListener = null;
let now = 0;
const commands = [];
const statuses = [];
class FakeDate extends Date {
  static now() { return now; }
}
const context = {
  console,
  Date: FakeDate,
  fetch: async () => ({
    ok: true,
    json: async () => ({
      espn: { pending_espn_player_id: "42", match_rate: 1 },
      settings: { override_seconds: 5 }
    })
  }),
  chrome: {
    storage: {
      local: {
        get: async () => ({ enabled: true, autoPickMocks: true }),
        set: async (value) => { statuses.push(value); }
      }
    },
    tabs: {
      sendMessage: async (_tabId, message) => { commands.push(message); },
      onRemoved: { addListener() {} }
    },
    runtime: {
      onMessage: { addListener: (listener) => { runtimeListener = listener; } }
    }
  }
};
vm.runInNewContext(fs.readFileSync("browser-companion/background.js", "utf8"), context);

const snapshot = {
  league_id: "MOCK",
  overall_pick: 6,
  on_clock: true,
  available_player_ids: ["42"]
};
function sendSnapshot() {
  return new Promise((resolve, reject) => {
    const keptOpen = runtimeListener(
      { type: "DRAFT_AGENT_SNAPSHOT", snapshot },
      { tab: { id: 9 } },
      (result) => result?.ok ? resolve() : reject(new Error(result?.error || "snapshot failed"))
    );
    if (keptOpen !== true) reject(new Error("Background listener did not keep the response open"));
  });
}

(async () => {
  await sendSnapshot();
  now = 4000;
  await sendSnapshot();
  if (commands.length !== 0) throw new Error("Mock pick fired before the override period");
  now = 6000;
  await sendSnapshot();
  if (commands.length !== 1 || commands[0].command.player_id !== "42") {
    throw new Error("Mock pick did not fire after the override period");
  }
  if (statuses.length !== 3) throw new Error("Each accepted snapshot should update status");
  console.log("background bridge tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

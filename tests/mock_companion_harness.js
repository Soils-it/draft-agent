"use strict";

const fs = require("fs");
const vm = require("vm");

const listeners = new Map();
const results = [];
let selectedId = null;
const player = {
  id: 42,
  fullName: "Test Runner",
  available: true,
  rankByEditorialDraftRank: 1,
  seasonProj: 300,
  primaryPosition: { abbrev: "RB" },
  proTeam: { abbrev: "BUF" }
};
const draft = {
  leagueId: "MOCK-LEAGUE",
  pickIndex: 5,
  teamId: 6,
  controllingTeam: { teamId: 6 },
  teams: [{ teamId: 6, draftOrder: 5 }],
  isMockLeague: false,
  isCurrentlyMyPick: () => true,
  sendSelectMessage: (id) => { selectedId = id; }
};
const store = { draft, playerPool: [player] };
const link = { __reactFiberForTest: { memoizedProps: { store } } };

class CustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
  }
}

const window = {
  addEventListener(type, callback) { listeners.set(type, callback); },
  dispatchEvent(event) {
    if (event.type === "ESPN_DRAFT_AGENT_MOCK_PICK_RESULT") results.push(event.detail);
    listeners.get(event.type)?.(event);
  }
};
const context = {
  window,
  document: { querySelector: () => link },
  CustomEvent,
  setInterval: () => 0,
  console
};
vm.runInNewContext(
  fs.readFileSync("browser-companion/page-observer.js", "utf8"),
  context
);

function command(overrides = {}) {
  window.dispatchEvent(new CustomEvent("ESPN_DRAFT_AGENT_MOCK_PICK", {
    detail: {
      mock_only: true,
      league_id: "MOCK-LEAGUE",
      overall_pick: 6,
      player_id: "42",
      ...overrides
    }
  }));
}

command();
if (selectedId !== null || results.at(-1)?.ok !== false) {
  throw new Error("A non-mock draft was not blocked");
}

draft.isMockLeague = true;
command({ overall_pick: 7 });
if (selectedId !== null || !results.at(-1)?.message.includes("stale")) {
  throw new Error("A stale mock pick was not blocked");
}

command();
if (selectedId !== 42 || results.at(-1)?.ok !== true) {
  throw new Error("A valid mock pick was not sent");
}

console.log("mock companion guard tests passed");

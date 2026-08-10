"use strict";

const fs = require("fs");
const vm = require("vm");

const listeners = new Map();
const results = [];
const snapshots = [];
const intervals = [];
let selectedId = null;
let selectionCalls = 0;
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
  sendSelectMessage: (id) => { selectedId = id; selectionCalls += 1; }
};
const store = { draft, playerPool: [player] };
let currentLink = { __reactFiberForTest: { memoizedProps: { store } } };

class CustomEvent {
  constructor(type, options = {}) {
    this.type = type;
    this.detail = options.detail;
  }
}

const window = {
  addEventListener(type, callback) {
    listeners.set(type, [...(listeners.get(type) || []), callback]);
  },
  dispatchEvent(event) {
    if (event.type === "ESPN_DRAFT_AGENT_MOCK_PICK_RESULT") results.push(event.detail);
    for (const callback of listeners.get(event.type) || []) callback(event);
  },
  postMessage(data) {
    if (data?.source === "ESPN_DRAFT_AGENT_PAGE" && data.type === "MOCK_PICK_RESULT") {
      results.push(data.result);
    }
    if (data?.source === "ESPN_DRAFT_AGENT_PAGE" && data.type === "SNAPSHOT") {
      snapshots.push(data.snapshot);
    }
    for (const callback of listeners.get("message") || []) callback({ source: window, data });
  }
};
const context = {
  window,
  document: { querySelector: () => currentLink, querySelectorAll: () => [] },
  CustomEvent,
  setInterval: (callback) => { intervals.push(callback); return intervals.length; },
  console
};
vm.runInNewContext(
  fs.readFileSync("browser-companion/page-observer.js", "utf8"),
  context
);

const freshDraft = { ...draft, pickIndex: 9 };
const freshStore = { draft: freshDraft, playerPool: [player] };
currentLink = { __reactFiberForTest: { memoizedProps: { store: freshStore } } };
intervals.at(-1)();
if (snapshots.at(-1)?.overall_pick !== 10) {
  throw new Error("The observer reused a stale React store after ESPN advanced the draft");
}
if (snapshots.at(-1)?.is_mock !== false) {
  throw new Error("The observer did not expose mock status in its snapshot");
}
const restartedDraft = { ...draft, leagueId: "NEW-MOCK", pickIndex: 0 };
currentLink = {
  __reactFiberForTest: { memoizedProps: { store: { draft: restartedDraft, playerPool: [player] } } }
};
intervals.at(-1)();
if (snapshots.at(-1)?.league_id !== "NEW-MOCK" || snapshots.at(-1)?.overall_pick !== 1) {
  throw new Error("The observer reused a completed draft store in a newly started mock");
}
currentLink = { __reactFiberForTest: { memoizedProps: { store } } };
vm.runInNewContext(
  fs.readFileSync("browser-companion/page-observer.js", "utf8"),
  context
);

function command(overrides = {}) {
  window.postMessage({
    source: "ESPN_DRAFT_AGENT_CONTENT",
    type: "MOCK_PICK",
    command: {
      mock_only: true,
      league_id: "MOCK-LEAGUE",
      overall_pick: 6,
      player_id: "42",
      ...overrides
    }
  });
}

command();
if (selectedId !== null || results.at(-1)?.ok !== false) {
  throw new Error("A non-mock draft was not blocked");
}

draft.isMockLeague = true;
intervals.at(-1)();
if (snapshots.at(-1)?.is_mock !== true) {
  throw new Error("The observer did not refresh mock status");
}
command({ overall_pick: 7 });
if (selectedId !== null || !results.at(-1)?.message.includes("stale")) {
  throw new Error("A stale mock pick was not blocked");
}

command();
if (selectedId !== 42 || selectionCalls !== 1 || results.at(-1)?.ok !== true) {
  throw new Error("A valid mock pick was not sent");
}
if (
  results.at(-1)?.draft_id !== "MOCK-LEAGUE" ||
  results.at(-1)?.overall_pick !== 6 ||
  results.at(-1)?.player_id !== "42" ||
  results.at(-1)?.name !== "Test Runner"
) {
  throw new Error("The successful mock result did not include decision-audit identity");
}

console.log("mock companion guard tests passed");

(() => {
  "use strict";

  const LOCAL_ENDPOINT = "http://127.0.0.1:8765/api/espn/snapshot";
  const REQUIRED_SELECTORS = [
    "overallPickSelector",
    "onClockSelector",
    "availablePlayerSelector",
    "rosterPlayerSelector"
  ];

  function queryAll(selector) {
    try {
      return Array.from(document.querySelectorAll(selector));
    } catch (_error) {
      throw new Error(`Invalid selector: ${selector}`);
    }
  }

  function playerIds(selector, attribute) {
    return [...new Set(queryAll(selector).map((element) => element.getAttribute(attribute)).filter(Boolean))];
  }

  function numberFromElement(selector) {
    const element = queryAll(selector)[0];
    if (!element) throw new Error(`No element matched ${selector}`);
    const match = element.textContent.match(/\d+/);
    if (!match) throw new Error(`No pick number found in ${selector}`);
    return Number(match[0]);
  }

  function onClockFromElement(selector) {
    const element = queryAll(selector)[0];
    if (!element) return false;
    const value = `${element.getAttribute("data-on-clock") || ""} ${element.textContent}`.toLowerCase();
    return /(^|\s)(true|your pick|on the clock)(\s|$)/.test(value);
  }

  function urlIdentifier(name) {
    return new URL(location.href).searchParams.get(name) || "";
  }

  async function saveStatus(status) {
    await chrome.storage.local.set({ draftAgentLastStatus: { ...status, at: new Date().toISOString() } });
  }

  async function sync() {
    const settings = await chrome.storage.local.get({
      enabled: false,
      playerIdAttribute: "data-player-id",
      overallPickSelector: "",
      onClockSelector: "",
      availablePlayerSelector: "",
      rosterPlayerSelector: ""
    });
    if (!settings.enabled) return;
    const missing = REQUIRED_SELECTORS.filter((key) => !settings[key]);
    if (missing.length) {
      await saveStatus({ ok: false, message: `Configure selectors: ${missing.join(", ")}` });
      return;
    }
    try {
      const snapshot = {
        league_id: urlIdentifier("leagueId"),
        draft_id: urlIdentifier("draftId") || urlIdentifier("draftLobbyId"),
        overall_pick: numberFromElement(settings.overallPickSelector),
        on_clock: onClockFromElement(settings.onClockSelector),
        available_player_ids: playerIds(settings.availablePlayerSelector, settings.playerIdAttribute),
        roster_player_ids: playerIds(settings.rosterPlayerSelector, settings.playerIdAttribute)
      };
      if (!snapshot.league_id || !snapshot.draft_id) {
        throw new Error("ESPN leagueId or draftId is missing from the page URL");
      }
      const response = await fetch(LOCAL_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Local bridge rejected the snapshot");
      await saveStatus({
        ok: true,
        message: "Shadow snapshot accepted; real submission remains disabled.",
        overallPick: snapshot.overall_pick,
        matchRate: result.espn.match_rate
      });
    } catch (error) {
      await saveStatus({ ok: false, message: String(error.message || error) });
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type !== "DRAFT_AGENT_SYNC") return false;
    sync().then(() => respond({ ok: true }), (error) => respond({ ok: false, error: String(error) }));
    return true;
  });

  setInterval(sync, 3000);
  sync();
})();

(() => {
  "use strict";

  const generation = Number(window.__ESPN_DRAFT_AGENT_PAGE_GENERATION__ || 0) + 1;
  window.__ESPN_DRAFT_AGENT_PAGE_GENERATION__ = generation;
  const isCurrent = () => window.__ESPN_DRAFT_AGENT_PAGE_GENERATION__ === generation;

  const PAGE_SOURCE = "ESPN_DRAFT_AGENT_PAGE";
  const CONTENT_SOURCE = "ESPN_DRAFT_AGENT_CONTENT";
  let cachedStore = null;

  function storesFromElement(element) {
    if (!element) return [];
    const reactKey = Object.keys(element).find((key) =>
      key.startsWith("__reactInternalInstance") || key.startsWith("__reactFiber")
    );
    const roots = reactKey ? [element[reactKey], element[reactKey]?.alternate] : [];
    const stores = [];
    for (const root of roots.filter(Boolean)) {
      let fiber = root;
      while (fiber && !fiber.memoizedProps?.store) fiber = fiber.return;
      const store = fiber?.memoizedProps?.store;
      if (store?.draft && store?.playerPool) stores.push(store);
    }
    return stores;
  }

  function findDraftStore() {
    const preferred = document.querySelector("a.player-news");
    const candidates = [
      preferred,
      ...Array.from(document.querySelectorAll("[class*='player'], [data-testid], button, a"))
    ];
    const stores = [];
    for (const element of new Set(candidates.filter(Boolean))) {
      stores.push(...storesFromElement(element));
    }
    cachedStore = stores.sort(
      (left, right) => Number(right.draft?.pickIndex ?? -1) - Number(left.draft?.pickIndex ?? -1)
    )[0] || cachedStore;
    return cachedStore;
  }

  function uniqueIds(players) {
    return [...new Set(players.map((player) => String(player.id)).filter(Boolean))];
  }

  function normalizePosition(value) {
    return value === "D/ST" ? "DST" : value;
  }

  function projectedPoints(player) {
    const candidates = [
      player?.seasonProj,
      player?.currentSeasonProjectedStats?.appliedTotal,
      player?.stattotal
    ];
    return candidates.map(Number).find(Number.isFinite) ?? 0;
  }

  function buildSnapshot() {
    const store = findDraftStore();
    if (!store) return null;
    const draft = store.draft;
    const pool = Array.from(
      { length: store.playerPool.length },
      (_, index) => store.playerPool[index]
    );
    const currentTeamId = Number(draft.controllingTeam?.teamId ?? draft.controllingTeam?.id);
    const userTeamId = Number(draft.teamId);
    const leagueId = String(draft.leagueId || "");
    if (!leagueId || !Number.isInteger(draft.pickIndex)) return null;
    const draftableAvailable = pool.filter((player) => {
      const rank = Number(player?.rankByEditorialDraftRank);
      return player?.available === true && Number.isFinite(rank) && rank <= 350;
    });
    const rankedPool = pool.filter((player) => {
      const rank = Number(player?.rankByEditorialDraftRank);
      return Number.isFinite(rank) && rank <= 350;
    });
    const teams = Array.from(
      { length: draft.teams?.length || 0 },
      (_, index) => draft.teams[index]
    );
    const userTeam = teams.find((team) => Number(team?.teamId ?? team?.id) === userTeamId);
    const draftOrder = Number(userTeam?.draftOrder);
    return {
      league_id: leagueId,
      draft_id: leagueId,
      overall_pick: draft.pickIndex + 1,
      on_clock: currentTeamId === userTeamId,
      is_mock: draft.isMockLeague === true,
      user_slot: Number.isInteger(draftOrder) ? draftOrder + 1 : null,
      player_catalog: rankedPool.map((player) => ({
        id: String(player.id),
        name: String(player.fullName || player.name || "Unknown player"),
        team: String(player.proTeam?.abbrev || "FA"),
        position: normalizePosition(String(player.primaryPosition?.abbrev || "")),
        rank: Number(player.rankByEditorialDraftRank),
        projected_points: projectedPoints(player)
      })),
      available_player_ids: uniqueIds(draftableAvailable),
      roster_player_ids: uniqueIds(
        pool.filter((player) => Number(player?.team?.teamId) === userTeamId)
      )
    };
  }

  function emitSnapshot() {
    if (!isCurrent()) return;
    const snapshot = buildSnapshot();
    if (snapshot) {
      window.postMessage({ source: PAGE_SOURCE, type: "SNAPSHOT", snapshot }, "*");
    }
  }

  function emitResult(detail) {
    window.postMessage({ source: PAGE_SOURCE, type: "MOCK_PICK_RESULT", result: detail }, "*");
  }

  function executeMockPick(command) {
    if (!isCurrent()) return;
    const store = findDraftStore();
    const draft = store?.draft;
    const fail = (message) => emitResult({ ok: false, message });
    if (!command?.mock_only || !draft) return fail("Draft state is unavailable.");
    if (draft.isMockLeague !== true) return fail("Selection blocked: this is not an ESPN mock draft.");
    if (String(draft.leagueId || "") !== String(command.league_id || "")) {
      return fail("Selection blocked: league changed.");
    }
    if (draft.pickIndex + 1 !== Number(command.overall_pick)) {
      return fail("Selection blocked: the pick is stale.");
    }
    if (typeof draft.isCurrentlyMyPick !== "function" || !draft.isCurrentlyMyPick()) {
      return fail("Selection blocked: you are no longer on the clock.");
    }
    const pool = Array.from(
      { length: store.playerPool.length },
      (_, index) => store.playerPool[index]
    );
    const selected = pool.find((player) => String(player?.id) === String(command.player_id));
    if (!selected || selected.available !== true) {
      return fail("Selection blocked: the recommended player is unavailable.");
    }
    if (typeof draft.sendSelectMessage !== "function") {
      return fail("Selection blocked: ESPN's mock selection control is unavailable.");
    }
    draft.sendSelectMessage(selected.id);
    emitResult({
      ok: true,
      message: `Mock pick sent: ${selected.fullName || selected.name}`,
      overall_pick: draft.pickIndex + 1,
      player_id: String(selected.id)
    });
  }

  window.addEventListener("message", (event) => {
    if (!isCurrent()) return;
    if (event.source !== window || event.data?.source !== CONTENT_SOURCE) return;
    if (event.data.type === "REQUEST_SNAPSHOT") emitSnapshot();
    if (event.data.type === "MOCK_PICK") executeMockPick(event.data.command);
  });
  setInterval(emitSnapshot, 2000);
  emitSnapshot();
})();

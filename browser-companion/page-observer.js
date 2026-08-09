(() => {
  "use strict";

  const SNAPSHOT_EVENT = "ESPN_DRAFT_AGENT_SNAPSHOT";
  const REQUEST_EVENT = "ESPN_DRAFT_AGENT_REQUEST";

  function findDraftStore() {
    const playerLink = document.querySelector("a.player-news");
    if (!playerLink) return null;
    const reactKey = Object.keys(playerLink).find((key) =>
      key.startsWith("__reactInternalInstance") || key.startsWith("__reactFiber")
    );
    let fiber = reactKey ? playerLink[reactKey] : null;
    while (fiber && !fiber.memoizedProps?.store) fiber = fiber.return;
    const store = fiber?.memoizedProps?.store;
    return store?.draft && store?.playerPool ? store : null;
  }

  function uniqueIds(players) {
    return [...new Set(players.map((player) => String(player.id)).filter(Boolean))];
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
    return {
      league_id: leagueId,
      draft_id: leagueId,
      overall_pick: draft.pickIndex + 1,
      on_clock: currentTeamId === userTeamId,
      available_player_ids: uniqueIds(draftableAvailable),
      roster_player_ids: uniqueIds(
        pool.filter((player) => Number(player?.team?.teamId) === userTeamId)
      )
    };
  }

  function emitSnapshot() {
    const snapshot = buildSnapshot();
    if (snapshot) {
      window.dispatchEvent(new CustomEvent(SNAPSHOT_EVENT, { detail: snapshot }));
    }
  }

  window.addEventListener(REQUEST_EVENT, emitSnapshot);
  setInterval(emitSnapshot, 2000);
  emitSnapshot();
})();

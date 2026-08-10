# ESPN Shadow Companion

This unpacked Manifest V3 browser extension observes an ESPN fantasy draft page
and posts snapshots to the local agent at `127.0.0.1:8765`. Its optional
automatic controller can submit picks only when ESPN's live draft store marks
the league as a mock draft.

The observer runs in ESPN's page context because player IDs are not present in
normal DOM attributes. It locates the React draft store from a rendered player,
then copies only the mock league ID, draft slot, current pick, on-clock status,
available and roster ESPN IDs, and the public draft-board fields needed by the
ranking engine: player name, NFL team, position, editorial rank, and projected
fantasy points. It never copies ESPN security tokens, member identifiers,
cookies, or credentials. The local bridge independently validates every entry
and rejects malformed snapshots or duplicate player IDs.

The available set is limited to ESPN's top 350 editorial draft ranks. This
avoids flooding the recommendation engine with hundreds of undrafted free
agents while retaining more than a full 12-team, 16-round draft pool. The local
agent precomputes five choices for the next user pick even when the user is not
currently on the clock.

## Development installation

1. Start the local server.
2. Open the browser's extensions page and enable Developer Mode.
3. Choose **Load unpacked** and select this directory.
4. Open an ESPN mock draft.
5. Enable syncing. Confirm the dashboard says **Connected in shadow mode**.
6. To test automatic drafting, separately enable **automatic picks in mock
   drafts**. Leave it off for observation-only use.

**Save and sync** reinjects the current unpacked scripts into the active ESPN
tab. This makes extension reloads reliable without depending on whether Chrome
kept older content scripts attached to an already-open draft page.

The controller waits for the configured override period. A manual ESPN pick
during that window makes its command stale and therefore harmless. Immediately
before sending, it checks mock status, league ID, overall pick, turn ownership,
and player availability. Real-draft selection is intentionally blocked.

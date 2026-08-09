# ESPN Shadow Companion

This unpacked Manifest V3 browser extension observes an ESPN fantasy draft page
and posts snapshots to the local agent at `127.0.0.1:8765`. It has no pick
submission code.

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

Do not enable real selection based on this companion. A future implementation
must add league/team verification, stale-state protection, a server-issued
one-time command, submission confirmation, and mock-draft evidence first.

# ESPN Shadow Companion

This unpacked Manifest V3 browser extension observes an ESPN fantasy draft page
and posts snapshots to the local agent at `127.0.0.1:8765`. Its optional
automatic controller can submit picks only when ESPN's live draft store marks
the league as a mock draft.

The observer runs in ESPN's page context because player IDs are not present in
normal DOM attributes. It locates the React draft store from a rendered player,
then copies only the league ID, mock-status flag, draft slot, current pick, on-clock status,
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

Version 0.7 sends the store's boolean mock-status flag so optional player
exposure limits can apply to practice drafts without affecting real-draft
recommendations.

## Development installation

1. Start the local server.
2. Open the browser's extensions page and enable Developer Mode.
3. Choose **Load unpacked** and select this directory.
4. Enable syncing once, then open an ESPN mock draft. The extension reconnects
   automatically when a draft page loads or the unpacked extension is reloaded.
5. Confirm the dashboard says **Connected in shadow mode** before the draft begins.
6. To test automatic drafting, separately enable **automatic picks in mock
   drafts**. Leave it off for observation-only use.

**Save and sync** remains a manual reconnect button. Normally the background
service worker injects one current page observer and one content bridge whenever
an ESPN draft page finishes loading, including an already-open draft when the
unpacked extension is reloaded.

Repeated syncs use generation ownership: the newest injected bridge supersedes
older page listeners. The content bridge only relays messages. Settings,
localhost requests, status updates, and override timing live in the Manifest V3
background service worker, so a page left open across an unpacked-extension
reload cannot keep using an invalid Chrome storage context.

The controller waits for the configured override period. A manual ESPN pick
during that window makes its command stale and therefore harmless. Immediately
before sending, it checks mock status, league ID, overall pick, turn ownership,
and player availability. Real-draft selection is intentionally blocked.

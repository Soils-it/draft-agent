# ESPN Shadow Companion

This unpacked Manifest V3 browser extension observes an ESPN fantasy draft page
and posts snapshots to the local agent at `127.0.0.1:8765`. It has no pick
submission code.

The selectors are intentionally blank. ESPN's draft markup must be inspected in
a mock draft before selectors are recorded; guessing selectors could silently
read the wrong player list. Configure selectors for:

- the element containing the current overall pick number
- the element indicating that your team is on the clock
- available-player rows
- your roster's player rows
- the attribute containing ESPN's numeric player ID

The companion refuses to sync until all selectors are configured. The local
bridge then independently rejects malformed snapshots, duplicate player IDs,
and mappings below 50%.

## Development installation

1. Start the local server.
2. Open the browser's extensions page and enable Developer Mode.
3. Choose **Load unpacked** and select this directory.
4. Open an ESPN mock draft.
5. Inspect and configure the four selectors.
6. Enable syncing. Confirm the dashboard says **Connected in shadow mode**.

Do not enable real selection based on this companion. A future implementation
must add league/team verification, stale-state protection, a server-issued
one-time command, submission confirmation, and mock-draft evidence first.

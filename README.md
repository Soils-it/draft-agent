# ESPN Fantasy Draft Agent

Local-first draft assistant for a 12-team ESPN full-PPR redraft league. The
current MVP provides a transparent ranking engine, snake-draft simulation, an
adjustable strategy, and a timed auto-pick flow. It starts with generated demo
players and can load a prior-season nflverse statistical baseline.

It does **not** log in to ESPN or submit real picks yet. That integration is
intentionally separated from the ranking engine so the strategy can be tested
in mock drafts first.

## League configuration

- 12 teams, snake draft
- 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 K, 1 D/ST
- 7 bench slots and 1 IR slot
- Full PPR with the custom passing, kicking, and D/ST scoring in
  `src/draft_agent/config.py`

The 16 active roster slots are drafted. IR is not treated as an extra draft
slot.

## Run locally

Python 3.9 or newer is supported. The MVP has no third-party runtime
dependencies.

```powershell
$env:PYTHONPATH = "src"
python -m draft_agent
```

Open <http://127.0.0.1:8765>. The dashboard defaults to draft slot 6, which can
be changed in Draft Settings. Opponent selections are simulated from ADP. Your
recommended player is picked after a configurable countdown (20 seconds by
default) unless you pause or choose an alternative.

The Player Data panel can download the selected regular season from the
[nflverse data releases](https://github.com/nflverse/nflverse-data/releases).
The files are free and CC BY 4.0 licensed. Downloaded CSVs are cached under
`.cache/`, which is ignored by Git. This baseline extrapolates prior per-game
production with conservative regression; it is clearly not a substitute for
current projections, injuries, depth charts, or true market ADP.

## ESPN bridge status

The server exposes `POST /api/espn/snapshot` for a future browser companion.
It accepts browser-observed league/draft identifiers, overall pick, on-clock
state, available ESPN player IDs, and roster ESPN player IDs. The bridge joins
those IDs through nflverse, rejects duplicates and low match rates, and returns
shadow recommendations. It intentionally exposes no submit-pick operation yet.

`browser-companion/` contains an optional unpacked extension for sending those
snapshots. Its read-only observer was verified against ESPN's 2026 mock-draft
React store. The companion is shadow-only and contains no code that clicks or
submits a player.

Example payload using placeholder IDs:

```json
{
  "league_id": "EXAMPLE_LEAGUE",
  "draft_id": "EXAMPLE_DRAFT",
  "overall_pick": 6,
  "on_clock": true,
  "available_player_ids": ["1001", "1002"],
  "roster_player_ids": []
}
```

## Test

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Ranking model

Each available player receives normalized component scores for:

- projected fantasy points
- value over replacement (VOR)
- positional scarcity and nearby tier drop
- roster need
- likelihood the player is gone before the next selection
- upside
- risk (subtracted)

Every weight is adjustable in the dashboard, and every recommendation exposes
its component scores. The model is deterministic for the same draft state and
settings.

## Next phase: ESPN integration

Add an adapter that reads the visible ESPN draft state from an already signed-in
browser session and submits a selected player only after verifying the league,
team, pick number, and available-player list. Passwords and session cookies must
never be stored by this project. Real selection should remain disabled until
the adapter passes ESPN mock-draft tests, timeout tests, and duplicate-pick
tests.

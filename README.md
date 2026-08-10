# ESPN Fantasy Draft Agent

Local-first draft assistant for a 12-team ESPN full-PPR redraft league. The
application provides a transparent ranking engine, snake-draft simulation, an
adjustable strategy, free current/historical data, and a timed mock-draft
auto-pick flow. Real-league automatic selection remains hard-blocked.

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

Use **Load complete free data** before opening a mock. The Player Data panel
downloads the selected regular season from the
[nflverse data releases](https://github.com/nflverse/nflverse-data/releases).
It then adds current full-PPR expert consensus and identity mappings from
[DynastyProcess open data](https://github.com/dynastyprocess/data), plus injury,
depth-chart, and 24-hour add/drop signals from the [Sleeper API](https://docs.sleeper.com/).
All downloads are size-bounded and cached under ignored `.cache/` directories.
No paid API key is required. ESPN's own projected points remain the primary
live-draft projection after the companion connects.

## ESPN bridge status

The server exposes `POST /api/espn/snapshot` for the browser companion. It
accepts browser-observed league/draft identifiers, draft slot, overall pick,
on-clock state, available and roster ESPN IDs, plus ESPN's current top-350
rankings and projected fantasy points. The bridge uses those current projections
for every player and enriches matching IDs with nflverse historical risk/upside.
Rookies, kickers, and D/ST entries no longer depend on a historical ID match.

Recommendations are calculated continuously for the user's next selection,
including while another team is on the clock. This makes a five-player queue
available before the turn begins. Automatic selection is disabled by default
and can be armed separately in the companion. It waits for the configured
override period and then revalidates the league, pick number, turn ownership,
player availability, and ESPN mock status before sending exactly one pick.
Real-league submission is hard-blocked in the page-context controller.

`browser-companion/` contains the unpacked extension that sends snapshots and
can submit the recommended player in ESPN mock drafts after the override timer.
The page controller revalidates draft identity, turn ownership, player
availability, and mock status immediately before submission. It will not
submit a pick in a real league.

Example payload using placeholder IDs:

```json
{
  "league_id": "EXAMPLE_LEAGUE",
  "draft_id": "EXAMPLE_DRAFT",
  "overall_pick": 6,
  "on_clock": true,
  "user_slot": 6,
  "player_catalog": [
    {
      "id": "1001",
      "name": "Example Player",
      "team": "BUF",
      "position": "RB",
      "rank": 1,
      "projected_points": 287.4
    }
  ],
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
- pick-relative full-PPR expert consensus, best-available market quality,
  analyst uncertainty, and reach cost
- value over replacement (VOR)
- positional scarcity and nearby tier drop
- roster need
- starting-lineup improvement and incumbent quality at every position
- likelihood the player is gone before the next selection
- injury/availability, bye-week fit, and Sleeper add/drop trends
- low-weight rookie camp role from depth-chart order and practice participation
- deterministic Monte Carlo value of this pick plus the next-turn options
- upside
- risk (subtracted)
- personal prefer/fade rules and optional mock exposure (when configured)

Every weight is adjustable in the dashboard, and every recommendation exposes
its raw component scores and signed weighted contributions. The model is
deterministic for the same draft state and settings.

Consensus is anchored to the current selection rather than normalized across
the full draft pool. This makes the difference between RB2 and WR10 meaningful
at pick 2. Best-available market quality preserves ordering between two players
who have both fallen, and a same-position dominance penalty favors the player
with meaningfully better consensus unless the alternative projects at least
10% higher. Projection, VOR, scarcity, tier drop, and turn simulation use smaller
weights because they are correlated views of the same underlying projection.
The default replacement baselines value RB42 versus WR36 to reflect the faster
loss of usable RB volume in this league. Reaches are limited to six picks in
round 1, ten in rounds 2-4, and receive an explicit score penalty. IR/PUP
players are excluded through round 12 when a healthy candidate is available.

The default 12-team, 1-QB roster-construction profile also prevents raw QB
scoring from dominating cross-position comparisons. It normally delays the
first QB until round 4, but permits a top-36 consensus QB earlier when that
player falls at least 12 picks below consensus. It blocks a backup QB through
round 12 and then requires that backup to have fallen at least 20 picks below
consensus. A top-48 overall incumbent QB suppresses QB2, and a top-60 incumbent
TE suppresses TE2, unless the user explicitly prefers the candidate. RB and WR
depth is evaluated by whether the candidate improves the actual 2RB/2WR/FLEX
lineup; non-starting depth receives diminishing value as the room fills. First
K and D/ST selections receive the same lineup-quality accounting, while their
position caps prevent redundant specialists. The opening remains value-based
between RB and WR, including an elite first-round WR. After a first-round WR, a
reasonably priced RB receives a strong round-2 anchor bonus. RB1/WR1 are
targeted by round 3 and a 2-RB/2-WR core by round 6, but the engine relaxes that
target instead of forcing a reach beyond ten picks. TE becomes available in
round 4, gains tier urgency through rounds 8-12, and is not forced until round
13. The profile reserves K and D/ST for rounds 15-16 and caps the planned RB
bench at five. A market guardrail limits reaches to six picks in round 1, ten
through round 4, 12 through round 8, 20 through round 12, and 35 afterward. RB
replacement value is calculated deeper than QB replacement value to reflect
two RB starters, FLEX demand, and the league's stronger RB scarcity.

Every ESPN decision is also written to the ignored local file
`.cache/draft_decisions.json`. Each record retains the roster and active rules,
top five overall candidates, best eligible candidate or blocking status for
QB/RB/WR/TE/K/DST, raw components, signed contributions, submitted player, and
whether the final roster addition matched the recommendation. Recent results
appear in the dashboard. Complete records are available at `/api/decisions`
and survive Python server restarts; at most 500 decisions are retained.

The Player Preferences panel accepts comma-separated **Prefer**, **Fade**, and
**Never draft** names. Prefer/fade adjustments are intentionally strong enough
to represent the user's player evaluation; never-draft entries are removed
from consideration. An optional exposure percentage applies only to ESPN mock
drafts and avoids players already selected at or above that rate across prior
mocks observed during the current server run. Keep exposure at 0% for the real
draft or when repeated best-value selections are desired. Player preferences
are persisted locally under the ignored `.cache/` directory; exposure history
resets with the Python server.

Rookie camp information is intentionally a supporting signal, not a primary
ranking. The free Sleeper feed supplies rookie experience, current depth-chart
order, practice participation, injury context, and 24-hour add/drop movement.
Those structured fields can move close decisions without allowing a single
camp report to overpower projections, consensus, or roster construction.

Monte Carlo trials are adjustable from 50 to 2,000 in Draft Settings. The
default 200-trial calculation is deterministic and normally completes well
inside a live pick clock.

## Historical backtesting

Prepare a CSV containing dated preseason inputs joined to final outcomes, then run:

```powershell
$env:PYTHONPATH = "src"
python -m draft_agent.backtest path\to\snapshots.csv --top 12
```

Required columns are `snapshot_date`, `season_start`, `player_id`, `name`,
`team`, `position`, `adp`, `projected_points`, and `actual_points`. Optional
columns are `consensus_rank`, `consensus_sd`, `bye_week`, and `injury_status`.
The runner rejects snapshots dated on or after the season start to prevent
outcome leakage, and reports actual-points and top-player hit-rate lift against
a projection-only baseline. Archived FantasyPros snapshots from DynastyProcess
and nflverse season results can be joined by the included provider IDs.

## Next phase: ESPN integration

Validate the guarded controller through repeated ESPN mock drafts, including
timeout, manual override, stale-state, and duplicate-pick scenarios. Passwords
and session cookies must never be stored by this project. Real selection remains
disabled until that evidence is complete and a separate real-draft safety review
is performed.

# ESPN Fantasy Draft Agent

Local-first draft assistant for 12-team ESPN full-PPR redraft leagues. The
application provides a transparent ranking engine, snake-draft simulation, an
adjustable strategy, free current/historical data, and a timed mock-draft
auto-pick flow. Real-league automatic selection remains hard-blocked.

## League profiles

- 12 teams, snake draft
- 7 bench slots and 1 IR slot
- Full PPR with the custom passing, kicking, and D/ST scoring in
  `src/draft_agent/config.py`

The dashboard includes three selectable formats:

- **12-team PPR · 1 QB** (default): 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX,
  1 K, 1 D/ST, and 7 bench spots (16 drafted players).
- **12-team PPR · Superflex**: Superflex replaces the normal FLEX, with the
  same seven bench spots (16 drafted players).
- **12-team PPR · FLEX + Superflex**: keeps the normal FLEX and adds a
  Superflex spot, with seven bench spots (17 drafted players).

All profiles share the same scoring, player projections, current news/injury
signals, adjustable weights, and player preferences. Only roster construction,
positional scarcity, replacement levels, and turn simulation change. IR is not
treated as an extra draft slot.

## Run locally

Python 3.9 or newer is supported. The MVP has no third-party runtime
dependencies.

```powershell
$env:PYTHONPATH = "src"
python -m draft_agent
```

Open <http://127.0.0.1:8765>. The dashboard defaults to the 1-QB profile and
draft slot 6; both can be changed in Draft Settings. The selection is saved in
`.cache/draft_settings.json` and restored with the server. Opponent selections
are simulated from the active profile's market. Your
recommended player is picked after a configurable countdown (20 seconds by
default) unless you pause or choose an alternative.

Choose the profile that exactly matches the ESPN roster before entering a mock
or real draft, then click **Apply settings**. A profile change resets the local
mock and deliberately invalidates the old ESPN snapshot. In the companion,
click **Save and sync** once so the new model receives a fresh board. Profile
detection is not inferred from ESPN yet because using the wrong 16- versus
17-round layout would make turn ownership unsafe.

On startup the server restores complete, validated nflverse and free-signal
caches without making a network request. Cache file modification times are used
as the required-source timestamps. A missing, partial, oversized, malformed, or
stale required cache is reported truthfully as **NOT READY**; generated demo
players are never labeled as loaded historical data. The separately cached
Vegas source is optional and never changes READY/NOT READY. Use **Load complete
free data** to refresh missing or stale data. Applying draft settings or
resetting the local mock keeps the active player pool, ESPN IDs, historical
baselines, current signals, preferences, and strategy weights.

Use **Load complete free data** before opening a mock. The Player Data panel
downloads the selected regular season from the
[nflverse data releases](https://github.com/nflverse/nflverse-data/releases).
It then adds current full-PPR 1-QB and Superflex/OP expert consensus plus identity mappings from
[DynastyProcess open data](https://github.com/dynastyprocess/data), plus injury,
depth-chart, and 24-hour add/drop signals from the [Sleeper API](https://docs.sleeper.com/).
It also explicitly refreshes the public nflverse
[schedules CSV](https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv)
and [release timestamp](https://github.com/nflverse/nflverse-data/releases/download/schedules/timestamp.json).
For future regular-season games with both lines, a positive `spread_line` means
the home team is favored, so `home implied = (total_line + spread_line) / 2` and
`away implied = (total_line - spread_line) / 2`. The app averages each team's
own and opponent implied points and retains the lined-game count for auditability.
This is team-level market context, not player props and not a replacement for
projections. nflverse data is attributed to **nflverse (nflverse-data)** under
the **CC BY 4.0** license.

All downloads are size-bounded and cached under ignored `.cache/` directories.
The normalized Vegas snapshot and provenance live separately at
`.cache/vegas/snapshot.json`. Both schedule and timestamp responses are fully
validated in memory, including all-32-team coverage, before one atomic cache
replacement. Missing, malformed, partial, oversized, or failed refreshes cannot
replace the last-known-good snapshot. A still-fresh cached snapshot may be used
with an explicit fallback error; after 48 hours it remains visible in source
health but contributes exactly zero until refreshed. No paid API key is
required. ESPN's own projected points remain the primary live-draft projection
after the companion connects.

## Draft readiness

The dashboard and `/api/state` expose one prominent **READY** or **NOT READY**
result with actionable reasons and per-source freshness. A live snapshot is
READY only when all of these checks pass:

- snake order and the observed roster agree, including every selection already
  confirmed during the active draft;
- the ESPN catalog has at least 100 players, at least 80% of available players
  map, and every roster player maps;
- at least 50% of the current ESPN catalog has a historical baseline (rookies,
  kickers, and D/ST may legitimately lack one);
- at least 75% has current consensus/injury signals, the local signal cache has
  adequate coverage, and those signals are no more than 48 hours old; and
- the latest ESPN snapshot is no more than 10 seconds old.

NOT READY is fail-closed: no pending player ID or mock command is produced.
For example, slot 1 at pick 24 requires the pick-1 player to appear on the
roster. An empty or regressed roster cannot re-offer that confirmed player.
Wait for the next companion snapshot, use **Save and sync**, or refresh complete
free data according to the displayed reason.

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

If ESPN rejects a mock command as stale or unavailable, the companion requests
one fresh snapshot and permits one new override-period arm; it never loops or
submits twice from the same command.

For a real draft, use the agent in shadow mode only: leave companion automatic
picks disabled, read the READY recommendation, and make the selection manually
in ESPN. The extension has no supported real-draft submission path, and enabling
mock automation does not relax that boundary.

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
- a 70% ESPN-room / 30% external-ECR market rank, source disagreement,
  best-available market quality, analyst uncertainty, and reach cost
- value over replacement (VOR)
- positional scarcity and nearby tier drop
- roster need
- starting-lineup improvement and incumbent quality at every position
- deep-bench opportunity cost based on the actual wait until the next pick
- likelihood the player is gone before the next selection
- injury/availability, bye-week fit, nonlinear NFL-team/bye concentration, and
  Sleeper add/drop trends
- a small, signed nflverse Vegas environment from team implied scoring for
  QB/RB/WR/TE/K and opponent implied scoring for D/ST
- low-weight rookie camp role from depth-chart order and practice participation
- deterministic Monte Carlo value of this pick plus the next-turn options
- upside
- risk (subtracted)
- personal prefer/fade rules and optional mock exposure (when configured)

Every weight is adjustable in the dashboard, and every recommendation exposes
its raw component scores and signed weighted contributions. The model is
deterministic for the same draft state and settings.

The Vegas component is centered on the 32-team league average and clamped to
`[-1, 1]`. Higher team implied scoring is positive for QB/RB/WR/TE/K; lower
opponent implied scoring is positive for D/ST. Its default weight is `0.03`, and
the engine and API enforce an absolute `0.03` contribution cap even if a client
submits a larger value. Unknown teams, missing player fields, incomplete team
coverage, stale snapshots, and unavailable data are neutral (`0`) and do not
change eligibility, roster deadlines, reach guardrails, projections, or
otherwise-identical score ordering.

Market rank blends ESPN's current draft-room order at 70% with external expert
consensus at 30%. This keeps useful independent information without allowing a
stale or strongly dissenting ECR to create a multi-round reach against the room
being drafted. The absolute source gap is also a visible, adjustable penalty.
Market value is anchored to the current selection rather than normalized across
the full draft pool, so the difference between RB2 and WR10 remains meaningful
at pick 2. Best-available market quality preserves ordering between two players
who have both fallen, and a same-position dominance penalty favors the player
with meaningfully better market support unless the alternative projects at
least 10% higher. Projection, VOR, scarcity, tier drop, and turn simulation use
smaller weights because they are correlated views of the same projection.
The default replacement baselines value RB42 versus WR36 to reflect the faster
loss of usable RB volume in this league. Reaches are limited to six picks in
round 1, ten in rounds 2-4, and receive an explicit score penalty. IR/PUP
players are excluded through round 12 when a healthy candidate is available.

The default 12-team, 1-QB roster-construction profile also prevents raw QB
scoring from dominating cross-position comparisons. It normally delays the
first QB until round 4, but permits a top-36 blended-market QB earlier when that
player falls at least 12 picks below market. It blocks a backup QB through
round 12 and then requires that backup to have fallen at least 20 picks below
consensus. A healthy top-90 overall incumbent QB suppresses QB2 unless the
candidate is at least 15 market spots better or projects at least 5% higher. A
weak or questionable/out starter can still unlock late QB insurance. A top-60
incumbent TE suppresses TE2. A later TE2 must be a meaningful market or
projection upgrade over a non-elite incumbent, cover an injury, or be
explicitly preferred by the user. RB and WR depth is evaluated by whether the
candidate improves the actual 2RB/2WR/FLEX lineup; non-starting depth receives
diminishing value as the room fills. First
K and D/ST selections receive the same lineup-quality accounting, while their
position caps prevent redundant specialists. The opening remains value-based
between RB and WR, including an elite first-round WR. After a first-round WR, a
reasonably priced RB receives a strong round-2 anchor bonus. RB1/WR1 are
targeted by round 3 and a 2-RB/2-WR core by round 5, but the engine relaxes that
target instead of forcing a reach beyond ten picks. TE becomes available in
round 4, gains tier urgency through rounds 7-12, and receives its strongest
starter-tier pressure beginning in round 10. It is not forced until round 13.
The profile reserves K and D/ST for rounds 15-16 and caps the planned RB
bench at five. A market guardrail limits reaches to six picks in round 1, ten
through round 4, 12 through round 8, 20 through round 12, and 35 afterward. RB
replacement value is calculated deeper than QB replacement value to reflect
two RB starters, FLEX demand, and the league's stronger RB scarcity.

The Superflex profiles use DynastyProcess's separate free Superflex/OP expert
consensus instead of treating ESPN's normal 1-QB overall board as Superflex
ADP. If that signal is unavailable for a player, the live ESPN catalog's stable
QB position order maps onto a conservative fallback curve. QB replacement
moves from QB12 to QB30, QB1 is required by round 3, and QB2 is required by
round 7. This allows a true elite quarterback to be an early-round value while
preventing raw projected points from making every QB an automatic reach. A
third QB is blocked before round 9 and is then considered only for a meaningful
fall, injury coverage, or an upgrade. Standard 1-QB deadlines, QB2 restrictions,
market blend, and opponent behavior remain unchanged when the default profile
is selected.

The team/bye concentration component is deliberately nonlinear. Adding a
second player from the same NFL team or bye week is a small tiebreaker; adding a
third receives a much larger penalty. It diversifies close decisions without
overriding a clearly superior second player.

Before round 12, a separate narrow tiebreaker discourages adding a second RB
from the same NFL backfield when an independently rostered RB is within five
market spots and 5% of the projection. A materially better value or an explicit
Prefer rule bypasses this penalty.

Roster depth also uses the real distance to the next selection. Adding RB5 or
WR7 receives a larger opportunity-cost penalty before a long wait—especially
while a core starter remains empty—and a smaller penalty before an adjacent
turn pick. This calculation uses snake-order distance, so it applies uniformly
to slots 1 through 12 rather than encoding a preferred draft position.

Every ESPN decision is also written to the ignored local file
`.cache/draft_decisions.json`. Each record retains the roster and active rules,
top five overall candidates, ESPN rank, external ECR, blended market rank,
source disagreement, best eligible candidate or blocking status for
QB/RB/WR/TE/K/DST, raw components, signed contributions, submitted player, and
whether the final roster addition matched the recommendation. Recent results
appear in the dashboard. The Mock Exposure panel summarizes the 20 most common
players across the last 12 completed mocks in this persisted audit. Complete
records are available at `/api/decisions` and survive Python server restarts;
at most 500 decisions are retained. The page
observer uses ESPN's own current-turn helper, and the local bridge independently
checks snake-order ownership. Impossible historical turn records are discarded
when the audit is loaded.

The Player Preferences panel accepts comma-separated **Prefer**, **Fade**, and
**Never draft** names. Prefer/fade adjustments are intentionally strong enough
to represent the user's player evaluation; never-draft entries are removed
from consideration. An optional exposure percentage applies only to ESPN mock
drafts and avoids players already selected at or above that rate across prior
mocks observed during the current server run. Keep exposure at 0% for the real
draft or when repeated best-value selections are desired; at 0%, both the hard
exposure filter and the soft exposure score are disabled. Player preferences
are persisted locally under the ignored `.cache/` directory. The optional cap's
active history resets with the Python server; the read-only exposure report is
rebuilt from the persisted decision audit.

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

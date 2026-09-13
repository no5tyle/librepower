# Local modifications to the vendored optimiser

Upstream: https://github.com/bolagnaise/powersync-optimiser (MIT)
Vendored at: `engine.py` — see `LICENSE.upstream`

Keep this file current. It is the only record of how far we've drifted from
upstream, and it's what makes pulling upstream fixes back in feasible.

## Applied

_None yet — `engine.py` is currently a verbatim copy._

## Planned

| # | Change | Why |
|---|--------|-----|
| 1 | Drop the `server.py` Flask layer | We call the engine in-process; no HTTP hop, no add-on container. Already excluded from the vendor copy. |
| 2 | Set a non-zero default `cycle_cost` | Upstream defaults to `0.0`, so the LP will happily cycle the battery for a fraction of a cent. Real LFP wear is ~1-3c/kWh throughput. |
| 3 | Export-price sign audit | Confirm negative feed-in (you pay to export) flows through the objective correctly — critical on Amber, which goes negative regularly. |
| 4 | Solve-time guard | Return the previous plan rather than blocking if a solve exceeds a few seconds. |
| 5 | Forecast-error headroom | Optionally reserve SOC margin against solar forecast shortfall, rather than trusting a point forecast. |
| 6 | Curtailment signal | LP currently has no notion of "block export this slot" - needed so the coordinator can drive `async_set_grid_export` from the plan itself rather than a bolt-on rule. |
| 7 | Islanding safety gate | Wrap `async_go_off_grid` with an SOC floor and daily duration cap before it is callable from anywhere automated - it currently has none. |

## Deliberately NOT porting from PowerSync

PowerSync's optimiser is PolyForm Noncommercial — its code cannot be copied
here. Any overlapping capability (EV co-optimisation, multi-battery, priority
export windows) must be implemented independently from published behaviour and
our own design, not transcribed from their source.

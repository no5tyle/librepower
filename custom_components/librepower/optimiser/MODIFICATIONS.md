# Local modifications to the vendored optimiser

Upstream: https://github.com/bolagnaise/powersync-optimiser (MIT)
Vendored at: `engine.py` — see `LICENSE.upstream`

Keep this file current. It is the only record of how far we've drifted from
upstream, and it's what makes pulling upstream fixes back in feasible.

## Applied

| # | Change | Why |
|---|--------|-----|
| 1 | Replaced the cvxpy/HiGHS LP formulation with a hand-built `scipy.optimize.milp` (HiGHS) formulation of the same problem — same decision variables, same constraints, same objective. | cvxpy's default solver stack (`osqp`, `clarabel`, `qdldl`, `sparsediffpy`) is an unconditional dependency of the `cvxpy` package, publishes no `musllinux` or 32-bit-ARM wheels, and a Home Assistant container ships no C/C++/Rust toolchain to build them from source. Installing librepower on a musl-libc host (Alpine-based HA image, confirmed via `platform.libc_ver()`) failed with an unrecoverable `RequirementsNotFound: cvxpy`, blocking config flow entirely even though `engine.py` already had a working heuristic fallback for when no solver is available. `scipy` has far broader wheel coverage (including `musllinux`) and already bundles HiGHS, so dropping cvxpy as the modeling layer removes the whole problem. Upstream (`bolagnaise/powersync-optimiser`) is archived, so there's no ongoing upstream to diverge from — this is a permanent fork, not a tracked delta. |
| 2 | Replaced the soft "simultaneous grid-charge/battery-export" penalty (`SIMULTANEOUS_CHARGE_EXPORT_PENALTY * cp.sum(cp.minimum(grid_to_battery, battery_to_grid))`) with an exact big-M constraint on a binary `mode[t]` variable per interval. | `cp.minimum()` of two affine expressions is concave, so adding it as a positive term inside a `cp.Minimize` objective isn't actually DCP-valid — the original comment admits it's "a heuristic penalty" chosen only because "we can't detect 'both non-zero' in LP." Since the rewrite already needed a solver capable of integer variables (`scipy.optimize.milp`), a binary `mode[t]` per interval enforces the exclusion exactly instead of approximately, with no penalty tuning required. |

## Planned

| # | Change | Why |
|---|--------|-----|
| 1 | Drop the `server.py` Flask layer | We call the engine in-process; no HTTP hop, no add-on container. Already excluded from the vendor copy. |
| 2 | Set a non-zero default `cycle_cost` | Upstream defaults to `0.0`, so the LP will happily cycle the battery for a fraction of a cent. Real LFP wear is ~1-3c/kWh throughput. |
| 3 | Export-price sign audit | Confirm negative feed-in (you pay to export) flows through the objective correctly — critical on Amber, which goes negative regularly. |
| 4 | Solve-time guard | Return the previous plan rather than blocking if a solve exceeds a few seconds. |
| 5 | Forecast-error headroom | Optionally reserve SOC margin against solar forecast shortfall, rather than trusting a point forecast. |
| 6 | Curtailment signal | LP currently has no notion of "block export this slot" - needed so the coordinator can drive `async_curtail_export`/`async_allow_export` from the plan itself rather than a bolt-on rule. |

Item 7 ("Islanding safety gate") is done - not listed here since it isn't a
change to the vendored engine at all. It landed in `librepower-powerwall`'s
`powerwall.py` as `PowerwallIslandingBlockedError`: `async_curtail_export`'s
strong-curtailment path now refuses to call `go_off_grid` if SOC is below a
floor (or unknown - fails closed) or the day's islanding-duration cap is
already used. The SOC floor is `max()`'d against this repo's own
`backup_reserve` option (wired through in the adapter's `__init__.py`) so a
user's configured minimum is never undercut for a more consequential action
than normal operation.

## Deliberately NOT porting from PowerSync

PowerSync's optimiser is PolyForm Noncommercial — its code cannot be copied
here. Any overlapping capability (EV co-optimisation, multi-battery, priority
export windows) must be implemented independently from published behaviour and
our own design, not transcribed from their source.

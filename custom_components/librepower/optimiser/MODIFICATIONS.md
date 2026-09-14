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
| 6 | Added a `curtailed_solar_w` decision variable (one per interval) to the "solar allocation" equality, so `solar_to_load + solar_to_battery + solar_to_grid + curtailed_solar = solar_forecast` instead of `... = solar_forecast` with no curtailment term. `OptimizationResult.curtailed_solar_w` exposes it; `coordinator.py`'s `_async_apply_export_curtailment` drives the adapter's `async_curtail_export("soft")`/`async_allow_export()` from it every tick. | Previously any solar exceeding load+battery capacity was *forced* into `solar_to_grid`, regardless of export price - grid was the only sink the model had. That made a negative export price (being charged to export) unavoidable even though real hardware can curb solar production at the inverter/Gateway level. `curtailed_solar` carries no cost or benefit of its own in the objective, so the solver only chooses it over exporting when exporting would cost more than curtailing (`p_export < 0`); it never curtails solar that could profitably be used or exported. Side effect (a genuine improvement, not a regression): a `max_grid_export_w` cap combined with high solar and a full battery could previously make the whole plan infeasible with no real fix available; it's now satisfiable via curtailment instead. |
| 8 | Added `OptimizationConfig.no_import_windows` (a list of `../time_windows.RecurringWindow`) + `no_import_max_w` + `tzinfo`, enforced in `_solve_lp` as one combined inequality row per affected interval (`grid_to_load + grid_to_battery <= no_import_max_w`, evaluated in site-local time via `tzinfo` - every timestamp elsewhere in the solver is UTC). | Recurring "avoid grid import entirely" windows are a real, general site-policy need (GloBird's ZeroHero evening-peak credit - a flat $1/day if you draw under ~0.03kWh from the grid during 6-9pm - is the motivating case, not upstream's own scope), independent of which pricing provider is active. Modeled as a hard cap rather than the exact threshold/bonus mechanic; the cap is a small nonzero wattage (200W default), not literally 0, since a literal 0 only *reduces* (doesn't eliminate) infeasibility risk on a night where load genuinely exceeds available battery + grid - the model's load balance is a hard equality with no unmet-load slack at all, so a shortfall beyond the allowance is still infeasible by design, the same as `max_grid_import_w` already could be. Caught and fixed during testing: an earlier draft bounded `grid_to_load`/`grid_to_battery` *separately* at 200W each (per-variable bounds), which actually permitted up to 400W combined - GloBird's rule measures total household draw, so this had to be a single combined inequality row instead. |

## Planned

| # | Change | Why |
|---|--------|-----|
| 1 | Drop the `server.py` Flask layer | We call the engine in-process; no HTTP hop, no add-on container. Already excluded from the vendor copy. |
| 5 | Forecast-error headroom | Optionally reserve SOC margin against solar forecast shortfall, rather than trusting a point forecast. |

Item 2 ("Set a non-zero default `cycle_cost`") is done - not added as its own
Applied row above since it isn't new behaviour, just a changed default: the
`cycle_cost` field itself already existed (used by the DCP-fix's mode
exclusivity work and everywhere else in `_solve_lp`), only its dataclass
default moved from `0.0` to `0.02`, matching `const.py`'s `DEFAULT_CYCLE_COST`
(already `0.02`, and what `__init__.py`/`config_flow.py` actually fill this
field with in production - see this file's own UI description text for the
1-3c/kWh reasoning shown to users). Upstream's `0.0` default let the LP cycle
the battery for a fraction of a cent of arbitrage; since `__init__.py` always
passes `cycle_cost` explicitly, the dataclass default was already dead in
production, but changed anyway so a future direct `OptimizationConfig()`
construction that omits it (a test, a script, a new caller) doesn't silently
regress to encouraging pointless cycling by omission - the two defaults now
match by construction, not by coincidence of one call site always overriding
the other.

Item 3 ("Export-price sign audit") is done - audited, not fixed, since it
found no bug. Confirmed against Amber's own documentation (negative feedIn
`per_kwh` = charged/debited for exporting, positive = paid) that the entire
pipeline - `entity_bridge.py`'s raw sensor read (no transform), `models.py`'s
`PriceInterval.export_price` (already documented as "positive when paid,
negative when charged"), `coordinator.py`'s passthrough into the optimiser,
and `engine.py`'s `export_revenue = p_export * grid_export` (subtracted from
cost, so a negative `p_export` correctly *increases* total cost) plus
`_calculate_baseline_cost` - is sign-consistent throughout, with no flip at
any stage. Verified empirically via `_calculate_baseline_cost` directly (a
surplus-solar scenario at a negative export price produces a positive
baseline cost, not a phantom credit, and the identical scenario at a
positive export price produces a lower cost) - note this audit originally
used a full-battery "forced export" scenario run through the optimizer
itself, but item 6 (curtailment, in the Applied table above) means that
scenario no longer forces export at all: the optimizer now correctly curtails the surplus instead of
paying to export it, landing `total_cost` at ~$0 rather than a positive
forced-export cost. That's the *correct* new behaviour, not a regression of
this audit - re-verified post-item-6 that the optimizer still exports (and
profits) when the export price is positive, and only curtails instead of
exporting when the price is negative, both directions consistent with the
sign convention above.

Item 4 ("Solve-time guard") is done - not listed above since, like item 7
below, it isn't a change to the vendored engine itself. `engine.py` already
had its own internal cap (`SOLVER_TIMEOUT = 30`, enforced via HiGHS's
`time_limit` option) and `coordinator.py` already kept the previous plan on
any solve failure - what was missing was an *outer* backstop, since the
internal cap only bounds HiGHS's own solve loop, not the problem-construction
time before it, nor a HiGHS/scipy release regression that fails to honour its
own `time_limit`. `coordinator.py`'s `async_refresh_plan` now wraps the
solve's executor call in `asyncio.wait_for(..., timeout=SOLVE_TIMEOUT_SECONDS)`
(45s - extra margin above the internal 30s cap); a timeout is treated the
same as any other solve failure (previous plan kept, error recorded). Note
this only stops *waiting* on the executor future, not the underlying thread
itself (Python's thread pool has no cancellation) - a truly wedged solve
still occupies one executor thread in the background, but the coordinator's
own async loop no longer hangs on it.

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

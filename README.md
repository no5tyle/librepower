# LibrePower

Local-first battery optimisation for Home Assistant. Core + battery adapters
+ pricing, split across repos on purpose — see "Why multiple repos" below.

No cloud account required for telemetry. No third-party proxy. No
subscription. This repo (core) contains the optimiser, coordinator, and
pricing; it has no idea what battery brand, if any, is attached until a
separate adapter integration registers one.

## Why multiple repos

The original single-repo design meant installing support for one battery
brand and one pricing provider pulled in updates for every *other* brand and
provider too — the exact HACS-update-noise problem this project set out to
avoid in the first place, and a real, observed pattern in the incumbent
alternative (PowerSync) this was compared against during design: commits for
Sungrow, GoodWe, and Sigenergy support landing in the same repo as commits
nobody using only a Powerwall would ever need.

The split:

| Part | Repo | Contains |
|---|---|---|
| **Core** (this repo) | `librepower` | Coordinator, optimiser, generic fixed-tariff and entity-bridge pricing, sensors, dashboard |
| **Battery** | `librepower-powerwall` (one repo per brand) | Everything brand-specific — protocol, device control, hardware config |
| **Provider** | *usually none needed* | Only for a retailer with neither an existing HA integration nor a usable public API — see "How pricing works" below |

Core defines a `BatteryClient` contract (`battery.py`) that any adapter repo
implements and registers against at runtime — core never imports a
brand-specific module. See that file's docstring for the actual cross-repo
mechanism and its honest limitations.

## Scope discipline

This project exists because the alternatives grew past the point where most
people could understand or trust them. The rules that keep that from happening
here:

1. **One battery brand per repo.** A second brand is a new adapter repo
   implementing `BatteryClient`, never a branch inside an existing one, and
   never inside core.
2. **Orchestrators don't compute.** `coordinator.py` wires modules together.
   Solvers, protocols, and forecasts live in their own files. If the
   coordinator grows maths, the maths is in the wrong place.
3. **No feature without a use case someone actually has.** Not "PowerSync has
   it." Config options are the main vector for complexity — each one doubles
   the states you have to reason about.
4. **Every sensor answers a real question.** Not one per internal variable.
5. **Cloud dependencies are opt-in, never required.** If a feature can't work
   offline, it's a separate optional module.

## Architecture (core)

```
custom_components/librepower/
├── __init__.py           setup / teardown, battery registration entry point
├── config_flow.py        guided setup: pricing only (no battery step)
├── const.py              all tunables in one place
├── coordinator.py        two loops: 30s telemetry, 5min re-solve
├── battery.py            the contract adapter repos implement - core imports
│                         no brand-specific module
├── load_forecast.py      per-slot median of observed load
├── solar_geometry.py     pure clear-sky elevation/GHI model, no network
├── solar_forecast.py     history-based solar learner (clear-sky-index)
├── open_meteo.py         optional weather-aware GHI, no key, opt-in
├── storage.py            shared persistence for both learners
├── sensor.py             9 sensors
├── optimiser/
│   ├── engine.py         vendored LP solver (MIT exception, see LICENSE.upstream)
│   ├── MODIFICATIONS.md  our delta vs upstream — keep current
│   └── __init__.py
└── pricing/
    ├── models.py         PriceForecast — the only type the optimiser sees
    ├── fixed_tariff.py   generic flat/ToU schedule, no network (formerly
    │                     "GloBird support" - it was always generic)
    └── entity_bridge.py  reads price sensors from an existing HA integration
                          (e.g. Amber's official one) instead of this project
                          maintaining its own retailer API clients
```

### Battery adapter interface

This is the published contract — everything a new battery-brand adapter
needs to implement is documented here and in `battery.py`'s own docstring
(same content, that's the source of truth if the two ever drift). Building
support for a new brand means writing an adapter repo that implements this
and nothing else; core never needs to change.

**How to connect:** declare `"dependencies": ["librepower"]` in your
manifest — this guarantees core loads first. Then, from your adapter's own
`async_setup_entry`, import core's types via Home Assistant's shared
`custom_components` namespace package and register:

```python
from custom_components.librepower.battery import BatteryClient, BatterySnapshot, ...
from custom_components.librepower import async_register_battery

await async_register_battery(hass, core_entry_id, my_client)
```

Until an adapter registers, core's sensors read `"waiting"` — that's the
normal state right after core is first set up, not an error. Only one
battery may be registered per core entry; a second attempt raises
`BatteryAlreadyRegisteredError` rather than silently replacing the first
(see "One battery per core entry" below).

#### Commands core sends (two independent channels)

**Battery disposition** — mutually exclusive, one active at a time:

| Method | Meaning |
|---|---|
| `async_charge(target_soc: float)` | Charge toward this SOC (0-1), forcing grid charge if your hardware needs that distinction from merely permitting it |
| `async_discharge(target_soc: float)` | Permit discharge down to this SOC, for load and/or export |
| `async_hold(soc: float)` | Pin at this SOC — no charge, no discharge |
| `async_release()` | Stop overriding. Return to the battery's own native automatic behaviour. Distinct from `hold`: hold pins a level, release lets go entirely — the correct signal for a clean handoff when LibrePower is disabled |

All three of charge/discharge/hold take a **target**, not a bare direction —
core's optimiser runs a real linear program to work out *how much*; a
directionless signal would throw that away and likely push every adapter
toward reinventing its own "how much is enough" logic. The real limitation
this doesn't solve: a single target per re-solve cycle can't express
*pacing* (gently over three hours vs. immediately at max rate) — accepted,
not fixed.

**Export policy** — independent of disposition, because they compose (e.g.
charging the battery *and* curtailing export simultaneously, to soak up
excess solar during a negative-price event without any of it reaching the
grid, is a real scenario a single combined enum can't express):

| Method | Meaning |
|---|---|
| `async_curtail_export(level: "soft" \| "strong")` | Reduce or block export. Named by *effect*, not mechanism — deliberately not Tesla's own `battery_ok/pv_only/never` vocabulary, since that doesn't map cleanly onto every brand's actual export-control primitive |
| `async_allow_export()` | Normal operation, no export restriction |

"Soft" should mean "stay grid-connected, just don't export" — however your
hardware achieves that. "Strong" means "block by any means necessary,"
including full islanding if that's genuinely what it takes. What either one
means mechanically is entirely up to your adapter.

#### Data core reads

```python
@dataclass
class BatterySnapshot:
    soc: float                        # 0.0-1.0
    solar_w: float                    # >= 0
    battery_w: float                  # + discharging, - charging
    grid_w: float                     # + importing, - exporting
    load_w: float                     # >= 0
    timestamp: datetime               # tz-aware; when this reading was taken
    grid_connected: bool = True       # verifies strong curtailment actually islanded
    operational_status: str = "ok"    # "ok" is the only value core treats specially;
                                       # report anything else as-is for faults/updates/etc
    state_of_health: float | None     # 0.0-1.0 degradation, None if your hardware can't report it
```

```python
@dataclass
class BatteryCapabilities:
    capacity_wh: float
    max_charge_w: float
    max_discharge_w: float
    charge_efficiency: float = 0.90     # pre-measurement default, see below
    discharge_efficiency: float = 0.90
```

Fetched once at registration and applied directly to the optimiser's config
— **not** typed into core's own setup. Capacity, charge/discharge limits,
and efficiency are properties of your specific battery; asking for them in
core, disconnected from which battery is actually attached, was the wrong
place for that config to live. Most local battery protocols (pypowerwall
included, confirmed by checking) have no way to read nameplate capacity from
the device — expect to collect these from the user during your own adapter's
setup, not auto-detect them.

Efficiency specifically is a **pre-measurement default, not a promise** — a
manufacturer nameplate figure, better than one number assumed for every
brand, but still static. A future telemetry-based learning feature is
expected to supersede this once enough real charge/discharge history exists;
how a learned value and an adapter-reported default reconcile isn't decided
yet.

#### Exceptions

All inherit from `BatteryError`. Raise the most specific one that applies —
core's coordinator catches the generic types and doesn't need to know which
brand or which specific cause is behind them:

| Exception | When |
|---|---|
| `BatteryAuthError` | Credentials rejected |
| `BatteryUnreachableError` | Can't contact the battery at all |
| `BatteryReadOnlyError` | A write was attempted in shadow mode — raise this yourself from every command method if `read_only` is set; core relies on the adapter enforcing this at the lowest level, not on a higher-level guard |
| `BatteryControlUnavailableError` | Control is enabled but you can't currently write, for whatever brand-specific reason (subclass this with your own more specific type — see `PowerwallV1rRequiredError` for the pattern) |
| `BatteryAlreadyRegisteredError` | Raised by core itself, not something an adapter raises |

#### One battery per core entry

Multiple *brands* (one repo each) is exactly what this split exists for.
Multiple *physical batteries combined under one site* is a different, harder
feature — real (Tesla sells multi-Powerwall systems) but out of scope until
a single battery is confirmed working end to end on real hardware. Two
independent core entries are today's workaround for two independently
managed batteries.

#### The cross-repo import mechanism, and its honest limitation

Home Assistant loads every custom_component under a shared
`custom_components` namespace package, so the import above works in
practice, and manifest `dependencies` guarantees load order. This is an
established community pattern, but it is **not an HA-core-blessed stable
API** — it relies on `custom_components` being importable-by-path, true
today but not a documented guarantee. Worth re-verifying if a future HA
release changes integration loading.

**Verified** (constructed `custom_components` namespace-package sandbox with
a minimal `homeassistant` stub, exercising the real cross-repo import path —
not an isolated test double): `PowerwallClient` genuinely satisfies
`BatteryClient` via `isinstance()`; every one of the six command methods is
correctly blocked in shadow mode and correctly maps to its Powerwall
mechanism (charge forces reserve above current SOC; discharge/hold set
reserve directly; release sets self-consumption mode; soft/strong
curtailment map to export-rule and islanding respectively); adapter-specific
exceptions are caught as core's generic types; capabilities including
efficiency propagate into the optimiser config on registration; a second
registration attempt is rejected rather than silently discarding the first.

**Not yet verified: an actual live two-integration Home Assistant install.**
The sandbox confirms the import graph and every documented behaviour is
sound — it does not confirm HA's real config-entry lifecycle, options-reload
timing, or storage behave as expected end to end.

### How pricing works

Both provider types reduce to a `PriceForecast` — a list of intervals with
import and export prices in $/kWh. The optimiser never knows which one is in
use, or which retailer, if any, is behind it.

- **Fixed tariff** (`pricing/fixed_tariff.py`): a flat rate or time-of-use
  schedule entered from the user's bill, generated locally. Zero network
  calls. Works for any flat/ToU retailer, not tied to a brand.
- **Entity bridge** (`pricing/entity_bridge.py`): reads price forecast data
  from an *existing* Home Assistant integration's sensors, rather than this
  project maintaining its own retailer API clients. Ships with a validated
  profile for Amber Electric's official core integration (confirmed against
  real community-reported attribute output, including that Amber's forecast
  regularly goes negative on feed-in — handled correctly, see
  `optimiser/MODIFICATIONS.md` item 3). A different integration's field names
  can be set in options if they differ from Amber's shape.

### Credits

Legal attribution is in `LICENSE`, `NOTICE`, and `optimiser/LICENSE.upstream`.
This section is the human version — the projects this one actually stands on.

- **[powersync-optimiser](https://github.com/bolagnaise/powersync-optimiser)**
  (MIT, © 2024 Ben Boller) — the vendored LP engine in `optimiser/engine.py`
  is this project's code, adapted to run in-process instead of as a Flask
  service.
- **[pypowerwall](https://github.com/jasonacox/pypowerwall)** (MIT, Jason Cox) —
  the local TEDAPI/v1r transport that makes cloud-free Powerwall telemetry
  possible at all. A runtime dependency, not vendored — full credit belongs
  with that project for the actual protocol reverse-engineering.
- **[Open-Meteo](https://open-meteo.com)** — free, no-key weather forecast
  data, used optionally to sharpen the solar forecast with actual weather
  rather than just seasonal climatology. Their generosity (10,000
  free non-commercial requests/day) is what makes an account-free,
  low-friction weather-aware mode possible at all.
- **[PowerSync](https://github.com/bolagnaise/PowerSync)** (bolagnaise) — prior
  art. No code is reused (PolyForm Noncommercial licensed, and LibrePower's
  architecture is intentionally much smaller), but its feature set and its
  actual production behaviour — including the export='never' curtailment
  approach and the v1r pairing requirement — were the reference point for
  understanding what a Powerwall integration needs to do.

## Licensing

LibrePower is **GPLv3 or later**. That's a deliberate choice, not just "the
free-est option": it guarantees that anyone who distributes a modified
version — including a commercial fork — must also make their source
available under GPL. MIT (what the project started as) doesn't provide that
guarantee; it explicitly allows closed derivatives. Given the whole reason
this project exists is frustration with a *source-available, use-restricted*
license, GPL's copyleft felt more consistent with that than staying purely
permissive.

Donations remain entirely separate from this: GPL says nothing about the
project's own funding model, and never requiring payment to *use* the
software is a property of essentially every OSI-approved license, GPL
included. A Sponsor/donate link is fine and unaffected by any of this.

**One file is an exception.** `optimiser/engine.py` is vendored from
[powersync-optimiser](https://github.com/bolagnaise/powersync-optimiser)
(MIT, © 2024 Ben Boller) and remains available under its original MIT terms —
see `optimiser/LICENSE.upstream`. This is permitted because MIT is
GPL-compatible in one direction: permissively-licensed code can be pulled
into a GPL work, with the combined result distributed under GPL and the
original MIT notice preserved for that file. It does not work the other way —
GPL code could not be pulled into an MIT project without the whole thing
becoming GPL. Full explanation in `NOTICE`.

**Nothing here derives from `bolagnaise/PowerSync`.** That project is PolyForm
Noncommercial — forkable for personal use but not redistributable
commercially. Overlapping capability must be implemented independently, from
published protocol documentation and our own design, never transcribed from
their source.

## Installing via HACS

Custom repositories, not in the default HACS store. Both are needed — core
alone has no idea what battery is attached; the adapter alone does nothing
without core.

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/no5tyle/librepower`, category **Integration**.
   Download, restart Home Assistant, then Settings → Devices & Services →
   **Add Integration** → LibrePower, and set up pricing (fixed tariff or
   entity bridge)
3. Add `https://github.com/no5tyle/librepower-powerwall` (or whichever
   battery brand applies), same custom-repository process, then **Add
   Integration** → LibrePower - Powerwall, and connect the Gateway

Order matters: core's config flow doesn't ask about batteries at all, so
setting it up first with nothing installed for step 3 yet is completely
normal — its sensors will simply read `"waiting"` until the battery adapter
registers.

## Running alongside PowerSync

Supported, and the recommended way to evaluate this. LibrePower ships in
**shadow mode**: it reads telemetry, fetches prices, and solves the full
schedule, but every write to the battery is blocked at the battery-adapter
level (e.g. `librepower-powerwall`'s `powerwall.py`, not in core, since core
has no battery client of its own). You get a
`sensor.librepower_planned_action` you can chart against what PowerSync
actually does, for as long as you want, with no risk of the two fighting.

**No collisions:**

| | PowerSync | LibrePower core | LibrePower Powerwall adapter |
|---|---|---|---|
| Domain | `power_sync` | `librepower` | `librepower_powerwall` |
| Entity prefix | `power_sync_*` | `librepower_*` | (no entities of its own) |
| Config entries | separate | separate | separate |

**What to watch for:**

- **Gateway polling.** Both PowerSync and the LibrePower Powerwall adapter
  poll the same Gateway. The adapter uses a 30s interval; if you see timeouts
  or TEDAPI errors in either integration, widen the telemetry interval in the
  adapter's own `const.py` before blaming the hardware.
- **Core no longer depends on `cvxpy`/`highspy`.** The vendored optimiser
  originally modelled the LP through `cvxpy` with the `highspy` (HiGHS)
  backend, but `cvxpy`'s solver stack (`osqp`, `clarabel`, `qdldl`,
  `sparsediffpy`) publishes no `musllinux` or 32-bit-ARM wheels, and a Home
  Assistant container has no compiler to build them from source — installs
  on those hosts failed outright. Core now solves the same LP directly via
  `scipy.optimize.milp` (HiGHS), which only needs `numpy`/`scipy` and
  installs cleanly on every platform HA itself supports. If PowerSync still
  pins `highspy` for its own optimiser, that's independent of core and not a
  shared-dependency concern anymore.
- **Only one integration may control the battery.** When you are ready to
  switch, disable PowerSync's optimiser *first*, confirm it has stopped
  writing, then enable control in core's options *and* make sure the battery
  adapter picks that setting up (see "Taking over control" below — this
  isn't automatically live-reloaded yet). Never both.

### Taking over control

Shadow mode is the default, decided in core's options, but actually
*enforced* in the registered battery adapter (e.g. `librepower-powerwall`'s
`powerwall.py`) — a write cannot escape even if a future service handler
calls the client directly, because core never holds a write-capable client of
its own.

**A real gap worth knowing about:** the adapter reads core's
`control_enabled` setting once, at the adapter's own setup time — it is not
currently live-reloaded if you change core's setting afterwards. Ticking
control on in core's options, behind the explicit confirmation screen, is
necessary but not by itself sufficient; the battery adapter integration
needs a manual reload (Settings → Devices & Services → LibrePower - Powerwall
→ reload) to pick up the new value. `sensor.librepower_control_mode` reports
`shadow`, `active`, or `waiting` (no battery registered yet) so the current
state is never ambiguous — check it after reloading, not just after changing
the option.

**What "active" actually does:** on each 5-minute re-solve, the coordinator
reads the LP's target SOC for the current slot and calls the registered
battery's `async_charge()`, `async_discharge()`, `async_hold()`, or
`async_release()` depending on the plan's current action — see "Battery
adapter interface" above for what each means and why they take a target SOC
rather than a bare direction. That's the disposition channel; the LP's
richer output (planned charge/discharge *rate*, specifically) isn't
commanded, only used to decide direction and target. The export-policy
channel is driven separately, every tick, from the LP's own
`curtailed_solar_w` signal: `async_curtail_export("soft")` when the plan
spills solar rather than exporting it that slot, `async_allow_export()`
otherwise — see `optimiser/MODIFICATIONS.md` item 6. Only "soft"
curtailment is ever requested this way; "strong" (intentional islanding) is
reserved for a safety/manual trigger, not an ordinary economic signal.

## Status

Scaffold. Not yet run against real hardware.

### Next steps

- [x] Confirm negative export prices flow correctly through the LP objective
      — audited, no bug found (see optimiser/MODIFICATIONS.md item 3);
      verified against Amber's own documented sign convention and empirically
      via `_calculate_baseline_cost` (updated post-curtailment; see that
      item's note on why the original forced-export scenario no longer
      applies once solar can be curtailed instead)
- [x] Set a realistic default `cycle_cost` — `OptimizationConfig.cycle_cost`'s
      own dataclass default moved from upstream's `0.0` to `0.02`, matching
      `const.py`'s `DEFAULT_CYCLE_COST` (already `0.02`) rather than relying
      solely on `__init__.py` always overriding it (see
      `optimiser/MODIFICATIONS.md`, item 2)
- [x] Solar forecast source — history-based clear-sky-index model, always on,
      zero network; Open-Meteo clearness-index adjustment, opt-in, no key
- [x] Write the plan back to the registered battery (backup-reserve control,
      active mode)
- [x] Split into core + battery-adapter repos, with a `BatteryClient`
      contract (`battery.py`) any brand implements against
- [x] No unregister path — `async_unregister_battery` added (core) and wired
      into `librepower-powerwall`'s `async_unload_entry`, called before the
      client closes, so a removed adapter no longer leaves the coordinator
      holding a stale, already-closed reference
- [x] Control-setting live-reload — `librepower-powerwall` now also listens
      on *core's* entry (not just its own), reloading itself whenever
      `control_enabled` or `backup_reserve` change in core's options
- [x] Fixed-tariff ToU windows in the options flow — `async_step_tou_windows`
      (add/remove/done menu, since HA's config flow has no native
      repeating-group widget); found and fixed a real bug while wiring this
      up: `default_import_price`/`default_export_price` were being read
      from `entry.options` but only ever written to `entry.data`, so every
      fixed-tariff setup silently got `0.0`/`0.0` and failed to construct
      at all - not something adding ToU windows caused, but making the ToU
      parser correct meant looking straight at the code that had this bug
- [x] Recurring "no-import" windows (`OptimizationConfig.no_import_windows`,
      `optimiser/MODIFICATIONS.md` item 8) - a general site policy shown
      regardless of pricing provider, motivated by GloBird's ZeroHero plan
      (a flat $1/day credit for staying under ~0.03kWh grid draw during
      evening peak); modeled as a near-zero hard cap on *total* grid draw
      rather than the exact threshold/bonus mechanic
- [x] Outer solve-time guard — `async_refresh_plan` now wraps the solve's
      executor call in `asyncio.wait_for(timeout=SOLVE_TIMEOUT_SECONDS=45s)`
      as a backstop on top of `engine.py`'s own internal 30s HiGHS
      `time_limit`; a timeout is treated the same as any other solve
      failure (previous plan kept, error recorded) rather than hanging the
      coordinator (see `optimiser/MODIFICATIONS.md` item 4)
- [x] Curtailment signal — the LP now has a `curtailed_solar_w` decision
      variable (item 6), and the coordinator drives
      `async_curtail_export("soft")`/`async_allow_export()` from it every
      tick, replacing the previously-unused channel
- [ ] Battery efficiency learned from telemetry (next item on the learning
      roadmap after solar — `charge_efficiency`/`discharge_efficiency` are
      still a hardcoded 0.90/0.90 in `OptimizationConfig`, never measured)
- [ ] Load forecast recency weighting (exponential decay toward recent days)
- [ ] Degradation/`cycle_cost` calibration from observed capacity fade

Powerwall-adapter-specific gaps (pypowerwall field verification against live
hardware, mocked-Gateway tests, the v1r pairing flow) now live in
[librepower-powerwall's own README](https://github.com/no5tyle/librepower-powerwall),
not here — core has no Gateway-specific code left to have gaps in.

## Solar forecasting

Two layers, composed by the coordinator — see `solar_forecast.py`,
`solar_geometry.py`, and `open_meteo.py` for the full design reasoning in
their docstrings.

**Always on, zero network:** `HistoricalSolarForecaster` learns a per-slot
"clear-sky index" from the site's own observed production divided by a
computed clear-sky shape (solar elevation geometry, no API, no key). This
self-calibrates for panel orientation, tilt, shading, and inverter clipping
without needing any of those as inputs — it's climatological ("what a
typical day in this season looks like here"), not a weather forecast.

**Optional, opt-in:** if enabled in options, Open-Meteo's forecast GHI
(no key, no account, ~10,000 free requests/day) is converted into a
clearness index — forecast GHI divided by the same clear-sky estimate — and
multiplied into the history-based forecast as a weather adjustment. Chosen
over Solcast deliberately: Solcast's free tier now caps new accounts at 10
API calls/day and requires an account plus a hand-paced polling automation,
which runs against this project's own low-friction, opt-in-cloud-dependency
rule. If Open-Meteo is unreachable, this falls back to history-only
automatically — `sensor.librepower_planned_action`'s `solar_forecast_source`
attribute reports which one actually produced the current forecast.

Both learners persist across restarts via `storage.py` (HA's `Store` helper),
saved hourly and on unload — fixing an earlier bug where `LoadForecaster`'s
`to_dict`/`from_dict` existed but were never actually wired to disk, silently
losing weeks of learned load history on every restart.

## Dashboard

A preconfigured Lovelace dashboard lives in `HA Dashboard/librepower_dashboard.yaml`
— battery/solar/load gauges, live power flows, current buy/sell price, a
24-hour schedule chart (planned SOC + planned charge/discharge), and the
optimiser's cost-vs-baseline for the current plan.

**One extra card required:** `apexcharts-card` via HACS → Frontend. That's
the whole dependency list — everything else is built into Home Assistant.
PowerSync's dashboard needs four extra cards (button-card, card-mod,
power-flow-card-plus, apexcharts-card); this needs one, because the schedule
chart is the only thing a stock card genuinely can't do — rendering an array
from an attribute as a time series.

Install: Settings → Dashboards → Add Dashboard → New dashboard from scratch →
three-dot menu → Edit in YAML → paste the file's content. Entity IDs assume a
single LibrePower config entry.

## Roadmap

**v0.1** Single repo, Powerwall + Amber, read-only plan as sensors.
**v0.2** Act on the plan via backup reserve.
**v0.3** GloBird ToU (generalised to fixed-tariff), solar forecast.
**v0.4** (current) Split into core + `librepower-powerwall`, generic
fixed-tariff and entity-bridge pricing replacing brand-specific clients.
**v1.0** Hardware-verified (a real two-repo HA install, not just the
namespace-package sandbox this was verified against), tested, documented.

A second battery brand no longer waits on v1.0 the way it used to — that was
true when everything lived in one repo and a new brand meant touching shared
code. Since the split, a second brand is a new adapter repo implementing
`battery.py`'s contract, independent of core's own release cycle. Worth
someone actually building one before calling that claim proven, though.

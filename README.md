# LibrePower

Local-first battery optimisation for Home Assistant. Powerwall + Amber/GloBird.

No cloud account. No third-party proxy. No subscription. The Gateway password
on your Powerwall is the only credential needed for battery control.

## Scope discipline

This project exists because the alternatives grew past the point where most
people could understand or trust them. The rules that keep that from happening
here:

1. **One battery brand at a time.** Powerwall works properly before anything
   else is started. A second brand is a new module implementing the same
   interface, not a new branch inside an existing one.
2. **Orchestrators don't compute.** `coordinator.py` wires modules together.
   Solvers, protocols, and forecasts live in their own files. If the
   coordinator grows maths, the maths is in the wrong place.
3. **No feature without a use case someone actually has.** Not "PowerSync has
   it." Config options are the main vector for complexity — each one doubles
   the states you have to reason about.
4. **Every sensor answers a real question.** Not one per internal variable.
5. **Cloud dependencies are opt-in, never required.** If a feature can't work
   offline, it's a separate optional module.

## Architecture

```
custom_components/librepower/
├── __init__.py           setup / teardown, wiring
├── config_flow.py        guided setup: Powerwall → retailer
├── const.py              all tunables in one place
├── coordinator.py        two loops: 30s telemetry, 5min re-solve
├── powerwall.py          local TEDAPI via pypowerwall (MIT dependency)
├── load_forecast.py      per-slot median of observed load
├── sensor.py             8 sensors
├── optimiser/
│   ├── engine.py         vendored LP solver (MIT exception, see LICENSE.upstream)
│   ├── MODIFICATIONS.md  our delta vs upstream — keep current
│   └── __init__.py
└── pricing/
    ├── models.py         PriceForecast — the only type the optimiser sees
    ├── amber.py          documented REST API
    └── globird.py        local ToU schedule, no network
```

### How the Powerwall connection works

pypowerwall's **gateway-password TEDAPI mode** for telemetry: HTTP to the
Gateway on your own network, authenticated with the password printed on the
unit. No Tesla account needed to *read* solar/battery/grid/load power and SOC.

**Control is a different story, and this was wrong in an earlier version of
this README.** Every write — backup reserve, operation mode, grid export
rule, islanding — requires pypowerwall's **v1r transport**, which needs an
RSA key registered through Tesla's Fleet API. That's a one-time cloud
handshake (physically confirmed by toggling the Gateway's DC isolator), not
an ongoing dependency, but it is real: gateway-password-only setup can plan a
schedule but cannot write it. This isn't a gap in this integration — it's how
Tesla's local protocol is designed, and it's the same step PowerSync itself
goes through for its own "local" control.

If a write is attempted without v1r, pypowerwall doesn't raise — it logs an
error and returns `None`, which would silently look like success. LibrePower
checks for this explicitly and raises `PowerwallV1rRequiredError`, surfaced
via `sensor.librepower_planned_action`'s status and throttled to one log line
per hour rather than every 5-minute tick.

**Solar curtailment**, since it's a common follow-up question: the Gateway
supports two real mechanisms, both v1r-gated.
- `async_set_grid_export("never")` — soft curtailment. Site stays
  grid-connected; export is simply forbidden, and the Gateway curtails solar
  production internally rather than overproduce with nowhere for the surplus
  to go. This is what PowerSync uses for DC-coupled (Tesla-integrated) solar.
- `async_go_off_grid()` — hard curtailment via full islanding. Same
  underlying throttling, but drops the site off-grid entirely (no import
  either). Reserved for cases the soft method can't reach — e.g. an
  AC-coupled inverter on a separate circuit that keeps exporting regardless
  of the Gateway's export rule. **This method has no safety gating of its own
  by design** — a caller needs to add its own SOC floor and duration cap
  before using it for anything automated. Not yet wired into the coordinator;
  `optimiser/MODIFICATIONS.md` tracks it as planned work.

Requirement either way: Home Assistant must have a network route to the
Gateway (`192.168.91.1` by default).

### How pricing works

Both retailers reduce to a `PriceForecast` — a list of intervals with import
and export prices in $/kWh. The optimiser never knows which retailer it is.

- **Amber**: documented REST API, 5-minute forward curve, regularly goes
  negative on feed-in (which the optimiser must handle correctly — see
  `MODIFICATIONS.md` item 3).
- **GloBird**: no public price API. Rates come from the user's bill as a
  time-of-use schedule and the curve is generated locally. Zero network calls.

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

This is a custom repository, not in the default HACS store.

1. HACS → three-dot menu → **Custom repositories**
2. URL: `https://github.com/YOURNAME/librepower`, category **Integration**
3. Download LibrePower, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → LibrePower

## Running alongside PowerSync

Supported, and the recommended way to evaluate this. LibrePower ships in
**shadow mode**: it reads telemetry, fetches prices, and solves the full
schedule, but every write to the battery is blocked at the client level. You
get a `sensor.librepower_planned_action` you can chart against what PowerSync
actually does, for as long as you want, with no risk of the two fighting.

**No collisions:**

| | PowerSync | LibrePower |
|---|---|---|
| Domain | `power_sync` | `librepower` |
| Entity prefix | `power_sync_*` | `librepower_*` |
| Config entries | separate | separate |

**What to watch for:**

- **Gateway polling.** Both integrations poll the same Gateway. LibrePower uses
  a 30s interval; if you see timeouts or TEDAPI errors in either integration,
  widen `UPDATE_INTERVAL_TELEMETRY` in `const.py` before blaming the hardware.
- **Shared dependency: `highspy`.** Both pin it, and pip installs one copy into
  the HA environment. LibrePower pins `>=1.7.0` to match PowerSync's floor so the
  resolver has no conflict to solve.
- **`cvxpy` is a heavy install** (pulls SciPy and a compiled solver stack).
  First startup after installing will be slow. This is the main cost of the
  vendored engine's modelling layer; dropping to raw `highspy` later would
  remove it.
- **Only one integration may control the battery.** When you are ready to
  switch, disable PowerSync's optimiser *first*, confirm it has stopped
  writing, then enable control in LibrePower's options. Never both.

### Taking over control

Shadow mode is the default and is enforced in `powerwall.py`, not in a
higher-level guard — a write cannot escape even if a future service handler
calls the client directly. To go live, tick control in the integration's
options, behind an explicit confirmation screen. `sensor.librepower_control_mode`
reports `shadow` or `active` so the current state is never ambiguous.

**What "active" actually does:** on each 5-minute re-solve, the coordinator
reads the LP's target SOC for the current slot and sets the Gateway's backup
reserve to it — raised above current SOC to force a charge, lowered to the
plan's floor to permit discharge. That's the one lever pulled; the LP's
richer output (planned charge/discharge *rate*, specifically) isn't
commanded, only used to decide direction. If reserve-only proves too coarse
in practice, that's the next control surface to add — not before.

## Status

Scaffold. Not yet run against real hardware.

### Next steps

- [ ] Verify `pypowerwall` telemetry field names against a live Gateway
- [ ] Confirm negative export prices flow correctly through the LP objective
- [ ] Set a realistic default `cycle_cost` (upstream ships `0.0`)
- [ ] Solar forecast source (Solcast, or Open-Meteo for a no-key option)
- [x] Write the plan back to the Gateway (backup-reserve control, active mode)
- [ ] GloBird ToU windows in the options flow
- [ ] Tests with a mocked Gateway

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

**v0.1** Powerwall + Amber, read-only plan as sensors.
**v0.2** Act on the plan via backup reserve.
**v0.3** GloBird ToU, solar forecast.
**v1.0** Hardware-verified, tested, documented.

Second battery brand comes after v1.0. Not before.

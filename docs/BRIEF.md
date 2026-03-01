# Smart EV Learning — Project Brief

## Goal

Build an adaptive consumption model for a Smart #1 EV that learns from real driving
history and recommends a morning charge target based on tomorrow's forecast conditions.

## Design Decisions

- **AppDaemon** chosen over Node-RED and HA automations for clean Python logic and
  testability without the HA runtime.
- **Exponential weighted average** (alpha=0.15) gives more weight to recent trips while
  retaining historical context. Older data decays gracefully.
- **Profile key** = `season|temp_band|drive_type|trip_type|preheating`. Approximately
  48 realistic profiles after deduplication of impossible season/band combos.
- **5-minute stop timer** handles traffic lights and short stops without splitting trips.
- **Fallback chain** in the predictor ensures a safe recommendation is always published,
  even when the exact profile has no data yet.
- **calc_basis** field distinguishes time-based vs km-based estimation for short cold
  trips where SOC drain is dominated by heating, not driving.

## Data Flow

```
sensor.smart_motor
       |
  ev_trip_logger.py
       | writes
  trips.json
       |
  model_updater.py
       | writes
  consumption_model.json
       |
  predictor.py
       | publishes
  sensor.ev_target_soc
  sensor.ev_prediction_status
```

## AppDaemon config path on HA host

| What | Host path | Container path |
|---|---|---|
| App scripts | `/addon_configs/a0d7b954_appdaemon/apps/` | `/config/apps/` |
| Trip data | `/addon_configs/a0d7b954_appdaemon/ev_trips/` | `/config/ev_trips/` |

Note: AppDaemon's `/config/` inside its container maps to `/addon_configs/a0d7b954_appdaemon/`
on the host — NOT to HA's main `/config/` (`/homeassistant/`).
The push script in claude-homeassistant repo writes to /homeassistant/ and is
irrelevant for AppDaemon script deployment.

## Startup behaviour

Both `ev_model_updater` and `ev_predictor` call their main function at the end of
`initialize()` so HA sensors are populated immediately on AppDaemon boot — no waiting
for the next hourly/daily schedule tick.

## Known deployment gotcha: `run_hourly` format

AppDaemon's `run_hourly(callback, start)` expects `start` in `"HH:MM:SS"` format or
omitted entirely. Passing `":00"` silently breaks callback registration. Use no
`start` argument to run every hour from startup.

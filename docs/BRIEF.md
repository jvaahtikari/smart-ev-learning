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

`/addon_configs/a0d7b954_appdaemon/apps/`

Note: this is different from `/homeassistant/apps/` (the HA config directory).
The push script in claude-homeassistant repo writes to /homeassistant/ and is
irrelevant for AppDaemon script deployment.

# Smart EV Learning

Adaptive EV consumption learning system for Home Assistant, built on AppDaemon.
Learns your actual driving patterns across different temperatures, seasons, and drive
types, then recommends a target charge level every morning so the car is always ready
without overcharging.

## What it does

Three AppDaemon apps work together: the **trip logger** watches the motor sensor and
records each drive segment with SOC used, distance, temperature, and driving style.
The **model updater** reads those trips and builds an exponentially weighted consumption
model grouped by season, temperature band, drive type, and trip length. The **predictor**
runs hourly, looks up tomorrow's forecast conditions, finds the matching profile, and
publishes a recommended target SOC to Home Assistant.

## Requirements

- Home Assistant (Core 2024.x+)
- AppDaemon add-on (Supervisor)
- Smart #1 / SmartHashtag HA integration

## Installation

1. Copy the three Python files to the AppDaemon apps directory on your HA host:

```bash
cp appdaemon/apps/*.py /addon_configs/a0d7b954_appdaemon/apps/
```

2. Add the app entries to AppDaemon's `apps.yaml`. See `docs/apps_yaml_snippet.yaml`.

3. Create the HA input helpers. See `docs/ha_helpers.yaml` — add these to your
   `configuration.yaml` or create them via the HA UI (Settings → Helpers).

4. Create the data directory on your HA host:

```bash
mkdir -p /homeassistant/ev_trips
```

5. Restart AppDaemon. Verify in the AppDaemon log that all three apps load without errors.

6. *(Optional)* Bootstrap with historical data:

```bash
python docs/prepopulate_from_csv.py history.csv /addon_configs/a0d7b954_appdaemon/apps/../../../data/ev_trips/trips.json
```

## Configuration

All entity names are defined as constants at the top of each script. Update them
to match your actual HA installation before deploying.

| Constant | Default entity | Description |
|---|---|---|
| `ENTITY_MOTOR` | `sensor.smart_motor` | Engine on/off — states: `engine_running`, `engine_off` |
| `ENTITY_BATTERY` | `sensor.smart_battery` | Battery SOC in % |
| `ENTITY_RANGE` | `sensor.smart_range` | Estimated range in km |
| `ENTITY_ODOMETER` | `sensor.smart_odometer` | Odometer in km |
| `ENTITY_PREHEAT` | `sensor.smart_pre_climate_active` | Preheating active |
| `ENTITY_CHARGER` | `sensor.zag063912_charger_mode` | Charger mode sensor |
| `ENTITY_WEATHER` | `weather.forecast_koti` | Weather entity for temperature |
| `ENTITY_AVG_SPEED` | `sensor.smart_average_speed` | Rolling trip average speed (km/h); resets at engine_on |
| `ENTITY_EXTERIOR_TEMP` | `sensor.smart_exterior_temperature` | Car's outdoor temperature sensor — used as primary `temp_actual` |
| `ENTITY_INTERIOR_TEMP` | `sensor.smart_interior_temperature` | Cabin temperature at engine_on — used for `preheat_temp_delta` and predictor preheat reserve |

> **Entity names:** The default entity names match the SmartHashtag integration naming
> convention but include a vehicle-specific identifier. Find your actual entity names
> by going to Developer Tools → States in Home Assistant and searching for "smart".
> Update the entity name constants at the top of each script to match your installation.

## Sensors published to HA

| Sensor | Description |
|---|---|
| `sensor.ev_target_soc` | Recommended charge target (%) |
| `sensor.ev_prediction_status` | Human-readable status message |
| `sensor.ev_learning_pct` | Model learning progress (%) |
| `sensor.ev_confidence` | Model confidence: collecting / learning / partial / reliable |
| `sensor.ev_last_trip_saved` | Timestamp of last recorded trip |

## Temperature bands

| Band | Range |
|---|---|
| jaakyma | below −15°C |
| cold | −15 to −5°C |
| near_zero | −5 to +2°C |
| cool | +2 to +10°C |
| mild | +10 to +15°C |
| normal | +15 to +25°C |
| hot | above +25°C |

## Data files (not committed — runtime only)

- `/config/ev_trips/trips.json` — all recorded trip segments
- `/config/ev_trips/consumption_model.json` — built model with EWA per profile

## Out of scope (future)

- Calendar integration for planned trip distance
- Dashboard UI cards
- Wind correction for highway trips
- ABRP API integration

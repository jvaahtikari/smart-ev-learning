"""
prepopulate_from_csv.py — Bootstrap trips.json from HA sensor history CSV.
Usage: python docs/prepopulate_from_csv.py <path_to_history.csv> [output_trips.json]

Expected CSV columns: entity_id, state, last_changed
Motor states: engine_running, engine_off, unavailable
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta

MOTOR         = "sensor.smart_motor"
BATTERY       = "sensor.smart_battery"
ODOMETER      = "sensor.smart_odometer"
RANGE_S       = "sensor.smart_range"
PREHEAT       = "sensor.smart_pre_climate_active"
WEATHER       = "weather.forecast_koti"
AVG_SPEED     = "sensor.smart_average_speed"
EXTERIOR_TEMP = "sensor.smart_exterior_temperature"
INTERIOR_TEMP = "sensor.smart_interior_temperature"

STOP_TIMEOUT_MIN = 5

SEASON_MONTHS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring",  4: "spring", 5: "spring",
    6: "summer",  7: "summer", 8: "summer",
    9: "autumn",  10: "autumn", 11: "autumn",
}

TEMP_BANDS = [
    (-999, -15, "jaakyma"),
    (-15,   -5, "cold"),
    (-5,     2, "near_zero"),
    (2,     10, "cool"),
    (10,    15, "mild"),
    (15,    25, "normal"),
    (25,   999, "hot"),
]


def get_temp_band(temp):
    for lo, hi, band in TEMP_BANDS:
        if lo <= temp < hi:
            return band
    return "normal"


def parse_ts(s):
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def load_csv(path):
    series = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entity = row["entity_id"].strip()
            state  = row["state"].strip()
            ts     = parse_ts(row["last_changed"].strip())
            series.setdefault(entity, []).append((ts, state))
    for k in series:
        series[k].sort(key=lambda x: x[0])
    return series


def nearest_float(series_list, ts, max_gap_hours=3):
    if not series_list:
        return None
    best = None
    best_gap = timedelta(hours=max_gap_hours + 1)
    for t, v in series_list:
        try:
            fv = float(v)
        except (ValueError, TypeError):
            continue
        gap = abs(t - ts)
        if gap < best_gap:
            best_gap = gap
            best = fv
    return best


def reconstruct_trips(series):
    motor_events  = series.get(MOTOR, [])
    battery       = series.get(BATTERY, [])
    odometer      = series.get(ODOMETER, [])
    range_est     = series.get(RANGE_S, [])
    preheat_s     = series.get(PREHEAT, [])
    avg_speed_s   = series.get(AVG_SPEED, [])
    exterior_s    = series.get(EXTERIOR_TEMP, [])
    interior_s    = series.get(INTERIOR_TEMP, [])

    # Find engine_running / engine_off transitions
    segments = []
    seg_start_ts = None

    for ts, state in motor_events:
        if state == "engine_running" and seg_start_ts is None:
            seg_start_ts = ts

        elif state == "engine_off" and seg_start_ts is not None:
            # Check for traffic stop: if next running within 5 min, skip
            idx = motor_events.index((ts, state))
            next_running = None
            for nts, nst in motor_events[idx + 1:]:
                if nst == "engine_running":
                    next_running = nts
                    break
                if nst == "engine_off":
                    break
            if next_running and (next_running - ts).total_seconds() <= STOP_TIMEOUT_MIN * 60:
                continue  # traffic stop, continue segment
            segments.append((seg_start_ts, ts))
            seg_start_ts = None

    trips = []
    for start_ts, end_ts in segments:
        soc_start = nearest_float(battery, start_ts)
        soc_end   = nearest_float(battery, end_ts)
        odo_start = nearest_float(odometer, start_ts)
        odo_end   = nearest_float(odometer, end_ts)
        range_val = nearest_float(range_est, start_ts)

        # Preheat: check if any preheat event is active near start
        preheat_state = None
        for t, v in preheat_s:
            if abs((t - start_ts).total_seconds()) < 1800:  # within 30 min
                preheat_state = v
        preheating = preheat_state not in (None, "off", "unavailable", "false", "0")

        if None in (soc_start, soc_end, odo_start, odo_end):
            continue

        distance_km  = odo_end - odo_start
        consumed_soc = soc_start - soc_end
        duration_min = (end_ts - start_ts).total_seconds() / 60

        if distance_km < 0.3 or duration_min < 1 or consumed_soc <= 0:
            continue

        # Use car's own average speed sensor at engine_off (more accurate)
        car_avg_speed = nearest_float(avg_speed_s, end_ts, max_gap_hours=0.1)
        avg_speed     = car_avg_speed if (car_avg_speed and car_avg_speed > 0) \
                        else (distance_km / duration_min) * 60
        drive_type   = "city" if avg_speed < 50 else ("mixed" if avg_speed < 80 else "highway")
        trip_type    = "short" if distance_km < 20 else "long"

        # Temperature: prefer car's exterior sensor; fall back to default for Feb data
        exterior_temp      = nearest_float(exterior_s, start_ts, max_gap_hours=1) or 0.0
        interior_temp_start = nearest_float(interior_s, start_ts, max_gap_hours=1) or 0.0
        # preheat_temp_delta: positive when cabin was warmer than exterior (preheat worked)
        preheat_temp_delta = round(interior_temp_start - exterior_temp, 1) if preheating else 0.0

        # Fall back to conservative default for Feb data (no weather in these CSVs)
        temp_actual  = exterior_temp if exterior_temp != 0.0 else -5.0
        calc_basis   = "time" if (trip_type == "short" and temp_actual < 5) else "km"

        # preheat_soc_cost: cannot be measured from CSV (no pre-preheat SOC snapshot)
        # Set None so model knows this field is unavailable for historical data
        preheat_soc_cost = None

        car_km_soc    = (range_val / soc_start) if (range_val and soc_start > 0) else 0
        actual_km_soc = distance_km / consumed_soc
        correction    = (car_km_soc / actual_km_soc) if actual_km_soc > 0 and car_km_soc > 0 else 0

        month  = start_ts.month
        season = SEASON_MONTHS.get(month, "winter")
        band   = get_temp_band(temp_actual)

        trips.append({
            "timestamp":            start_ts.strftime("%Y-%m-%dT%H:%M:%S"),
            "season":               season,
            "temp_band":            band,
            "temp_actual":          temp_actual,
            "exterior_temp":        round(exterior_temp, 1),
            "interior_temp_start":  round(interior_temp_start, 1),
            "winter_tyres":         True,  # assume winter tyres for Feb data
            "distance_km":          round(distance_km, 2),
            "duration_min":         round(duration_min, 1),
            "trip_type":            trip_type,
            "drive_type":           drive_type,
            "calc_basis":           calc_basis,
            "soc_start":            round(soc_start, 1),
            "soc_end":              round(soc_end, 1),
            "consumed_soc":         round(consumed_soc, 1),
            "preheating":           preheating,
            "plugged_in_preheat":   False,
            "preheat_temp_delta":   preheat_temp_delta,
            "preheat_soc_cost":     preheat_soc_cost,
            "range_estimate_start": round(range_val, 1) if range_val else 0,
            "car_km_per_soc":       round(car_km_soc, 2),
            "actual_km_per_soc":    round(actual_km_soc, 2),
            "correction_factor":    round(correction, 3),
        })

    return trips


def merge_series(a, b):
    """Merge two series dicts, combining lists and re-sorting by timestamp."""
    merged = dict(a)
    for entity, events in b.items():
        if entity in merged:
            merged[entity] = sorted(merged[entity] + events, key=lambda x: x[0])
        else:
            merged[entity] = events
    return merged


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python prepopulate_from_csv.py <history1.csv> [history2.csv ...] [--out trips.json]")
        sys.exit(1)

    args     = sys.argv[1:]
    out_path = "trips_prepopulated.json"
    csv_files = []
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_path = args[i + 1]; i += 2
        else:
            csv_files.append(args[i]); i += 1

    series = {}
    for csv_path in csv_files:
        print(f"Loading {csv_path} ...")
        series = merge_series(series, load_csv(csv_path))
    print(f"Entities found: {list(series.keys())}")

    trips = reconstruct_trips(series)
    print(f"Reconstructed {len(trips)} valid trips")

    for t in trips:
        print(f"  {t['timestamp']}  {t['distance_km']:.1f} km  "
              f"{t['consumed_soc']:.1f}% SOC  {t['drive_type']}/{t['trip_type']}"
              f"  ext={t['exterior_temp']}C  int={t['interior_temp_start']}C"
              f"  preheat_delta={t['preheat_temp_delta']}C")

    with open(out_path, "w") as f:
        json.dump(trips, f, indent=2)

    print(f"\nWritten to {out_path}")

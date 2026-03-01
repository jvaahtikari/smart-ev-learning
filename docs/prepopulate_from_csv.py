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

MOTOR    = "sensor.smart_motor"
BATTERY  = "sensor.smart_battery"
ODOMETER = "sensor.smart_odometer"
RANGE_S  = "sensor.smart_range"
PREHEAT  = "sensor.smart_pre_climate_active"
WEATHER  = "weather.forecast_koti"

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
    motor_events = series.get(MOTOR, [])
    battery      = series.get(BATTERY, [])
    odometer     = series.get(ODOMETER, [])
    range_est    = series.get(RANGE_S, [])
    preheat_s    = series.get(PREHEAT, [])

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

        avg_speed    = (distance_km / duration_min) * 60
        drive_type   = "city" if avg_speed < 50 else ("mixed" if avg_speed < 80 else "highway")
        trip_type    = "short" if distance_km < 20 else "long"

        # Use 10°C as default temp (winter data, no weather in CSV)
        temp_actual  = -5.0  # conservative default for Feb data
        calc_basis   = "time" if (trip_type == "short" and temp_actual < 5) else "km"

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
            "range_estimate_start": round(range_val, 1) if range_val else 0,
            "car_km_per_soc":       round(car_km_soc, 2),
            "actual_km_per_soc":    round(actual_km_soc, 2),
            "correction_factor":    round(correction, 3),
        })

    return trips


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python prepopulate_from_csv.py <history.csv> [trips.json]")
        sys.exit(1)

    csv_path  = sys.argv[1]
    out_path  = sys.argv[2] if len(sys.argv) > 2 else "trips_prepopulated.json"

    print(f"Loading {csv_path} ...")
    series = load_csv(csv_path)
    print(f"Entities found: {list(series.keys())}")

    trips = reconstruct_trips(series)
    print(f"Reconstructed {len(trips)} valid trips")

    for t in trips:
        print(f"  {t['timestamp']}  {t['distance_km']:.1f} km  "
              f"{t['consumed_soc']:.1f}% SOC  {t['drive_type']}/{t['trip_type']}")

    with open(out_path, "w") as f:
        json.dump(trips, f, indent=2)

    print(f"\nWritten to {out_path}")

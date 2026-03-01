"""
model_updater.py — EV Consumption Model Builder
Reads trips.json, computes exponentially weighted averages per profile,
writes consumption_model.json and publishes learning stats to HA.
"""

import json
import math
import os
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

VALID_SEASON_BANDS = {
    "winter":  ["jaakyma", "cold", "near_zero", "cool"],
    "spring":  ["near_zero", "cool", "mild"],
    "summer":  ["mild", "normal", "hot"],
    "autumn":  ["cool", "mild", "near_zero"],
}

ALL_DRIVE_TYPES  = ["city", "mixed", "highway"]
ALL_TRIP_TYPES   = ["short", "long"]
ALL_PREHEAT_KEYS = ["preheated", "cold_start"]


def _all_realistic_profiles():
    profiles = set()
    for season, bands in VALID_SEASON_BANDS.items():
        for band in bands:
            for dt in ALL_DRIVE_TYPES:
                for tt in ALL_TRIP_TYPES:
                    for pk in ALL_PREHEAT_KEYS:
                        profiles.add(f"{season}|{band}|{dt}|{tt}|{pk}")
    return profiles


def _profile_key(trip):
    preheat_key = "preheated" if trip.get("preheating") else "cold_start"
    return (f"{trip['season']}|{trip['temp_band']}|"
            f"{trip['drive_type']}|{trip['trip_type']}|{preheat_key}")


def build_model(trips, alpha=0.15, min_trips=5):
    """
    Pure function — build consumption model from list of trip dicts.
    Returns (model_dict, learning_pct, system_state).
    """
    # Group trips by profile key
    groups = {}
    for trip in trips:
        key = _profile_key(trip)
        groups.setdefault(key, []).append(trip)

    model = {}
    for key, group in groups.items():
        # Sort oldest → newest
        group_sorted = sorted(group, key=lambda t: t["timestamp"])

        km_soc_values  = [t["actual_km_per_soc"] for t in group_sorted if t["actual_km_per_soc"] > 0]
        soc_min_values = [(t["consumed_soc"] / t["duration_min"])
                          for t in group_sorted
                          if t["duration_min"] > 0 and t["consumed_soc"] > 0]
        corr_values    = [t["correction_factor"] for t in group_sorted if t["correction_factor"] > 0]

        count = len(km_soc_values)

        km_per_soc_ewa  = _ewa(km_soc_values, alpha)
        soc_per_min_ewa = _ewa(soc_min_values, alpha)
        correction_ewa  = _ewa(corr_values, alpha)
        std_dev         = _std(km_soc_values)

        confidence = (
            "missing"     if count == 0 else
            "preliminary" if count < 3  else
            "low"         if count < 5  else
            "high"
        )
        ready = count >= min_trips

        model[key] = {
            "count":           count,
            "km_per_soc_ewa":  round(km_per_soc_ewa, 3)  if km_per_soc_ewa  else None,
            "soc_per_min_ewa": round(soc_per_min_ewa, 4) if soc_per_min_ewa else None,
            "correction_ewa":  round(correction_ewa, 3)  if correction_ewa  else None,
            "std_dev":         round(std_dev, 3)          if std_dev         else None,
            "confidence":      confidence,
            "ready":           ready,
        }

    all_profiles    = _all_realistic_profiles()
    reliable_count  = sum(1 for p in all_profiles if model.get(p, {}).get("ready", False))
    learning_pct    = round((reliable_count / len(all_profiles)) * 100)

    system_state = (
        "collecting" if learning_pct == 0  else
        "learning"   if learning_pct < 40  else
        "partial"    if learning_pct < 80  else
        "reliable"
    )

    return model, learning_pct, system_state


def _ewa(values, alpha):
    if not values:
        return None
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def _std(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


class EVModelUpdater(hass.Hass):

    def initialize(self):
        self.log("EVModelUpdater starting")
        self.trips_file = self.args.get("trips_file", "/config/ev_trips/trips.json")
        self.model_file = self.args.get("model_file", "/config/ev_trips/consumption_model.json")
        self.min_trips  = int(self.args.get("min_trips_per_profile", 5))
        self.alpha      = float(self.args.get("learning_alpha", 0.15))

        self.listen_state(self._on_trip_saved, "sensor.ev_last_trip_saved")
        self.run_daily(self._rebuild, "02:00:00")
        self.log("Listening for new trips and scheduled daily rebuild at 02:00")
        # Run once immediately at startup to process any pre-existing trips.json
        self._rebuild({})

    def _on_trip_saved(self, entity, attribute, old, new, kwargs):
        self._rebuild({})

    def _rebuild(self, kwargs):
        if not os.path.exists(self.trips_file):
            self.log("No trips file yet — skipping rebuild")
            return

        try:
            with open(self.trips_file) as f:
                trips = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            self.log(f"Cannot read trips file: {exc}", level="WARNING")
            return

        model, learning_pct, system_state = build_model(trips, self.alpha, self.min_trips)

        model_out = {
            "built_at":     datetime.now().isoformat(),
            "trip_count":   len(trips),
            "learning_pct": learning_pct,
            "system_state": system_state,
            "profiles":     model,
        }

        os.makedirs(os.path.dirname(self.model_file), exist_ok=True)
        with open(self.model_file, "w") as f:
            json.dump(model_out, f, indent=2)

        self.set_state("sensor.ev_learning_pct", state=learning_pct,
                       attributes={"unit_of_measurement": "%", "friendly_name": "EV Learning Progress"})
        self.set_state("sensor.ev_confidence", state=system_state,
                       attributes={"friendly_name": "EV Model Confidence"})

        self.log(f"Model rebuilt: {len(trips)} trips, {learning_pct}% learning, state={system_state}")

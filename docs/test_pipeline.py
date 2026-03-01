"""
test_pipeline.py — End-to-end test for EV learning pipeline.
Runs without AppDaemon. Tests model_updater and predictor logic directly.
Usage: python docs/test_pipeline.py
"""

import json
import sys
import os
import types

# ---------------------------------------------------------------------------
# Stub out AppDaemon so pure functions can be imported without the daemon
# ---------------------------------------------------------------------------
def _make_stub():
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hass_pkg = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:
        pass

    hassapi.Hass = Hass
    ad.plugins = plugins
    plugins.hass = hass_pkg
    hass_pkg.hassapi = hassapi
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hass_pkg
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

_make_stub()

# Allow importing from appdaemon/apps without installing AppDaemon
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "appdaemon", "apps"))

from model_updater import build_model
from predictor import lookup_profile, compute_target, get_temp_band, get_season

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    results.append(condition)


# ---------------------------------------------------------------------------
# Step 1 — Synthetic trips covering 3 profiles
# ---------------------------------------------------------------------------
print("\n=== Step 1: Synthetic trips ===")

synthetic_trips = [
    # Profile A: winter|cold|city|short|cold_start  (5 trips → ready)
    {
        "timestamp": f"2026-01-0{i}T08:00:00", "season": "winter", "temp_band": "cold",
        "temp_actual": -10.0, "winter_tyres": True, "distance_km": 5.0 + i * 0.1,
        "duration_min": 12.0, "trip_type": "short", "drive_type": "city",
        "calc_basis": "time", "soc_start": 70, "soc_end": 60, "consumed_soc": 10.0,
        "preheating": False, "plugged_in_preheat": False,
        "range_estimate_start": 140.0, "car_km_per_soc": 2.0,
        "actual_km_per_soc": round((5.0 + i * 0.1) / 10.0, 3),
        "correction_factor": round(2.0 / ((5.0 + i * 0.1) / 10.0), 3),
        "avg_power_raw": 72.5,
    }
    for i in range(5)
] + [
    # Profile B: winter|near_zero|mixed|short|cold_start  (3 trips → low confidence)
    {
        "timestamp": f"2026-02-0{i+1}T08:00:00", "season": "winter", "temp_band": "near_zero",
        "temp_actual": 0.0, "winter_tyres": True, "distance_km": 12.0,
        "duration_min": 15.0, "trip_type": "short", "drive_type": "mixed",
        "calc_basis": "km", "soc_start": 65, "soc_end": 58, "consumed_soc": 7.0,
        "preheating": False, "plugged_in_preheat": False,
        "range_estimate_start": 160.0, "car_km_per_soc": 2.46,
        "actual_km_per_soc": round(12.0 / 7.0, 3),
        "correction_factor": round(2.46 / (12.0 / 7.0), 3),
        "avg_power_raw": 68.0,
    }
    for i in range(3)
] + [
    # Profile C: spring|cool|highway|long|cold_start  (2 trips → preliminary)
    {
        "timestamp": f"2026-03-1{i}T10:00:00", "season": "spring", "temp_band": "cool",
        "temp_actual": 6.0, "winter_tyres": False, "distance_km": 85.0,
        "duration_min": 60.0, "trip_type": "long", "drive_type": "highway",
        "calc_basis": "km", "soc_start": 80, "soc_end": 55, "consumed_soc": 25.0,
        "preheating": False, "plugged_in_preheat": False,
        "range_estimate_start": 200.0, "car_km_per_soc": 2.5,
        "actual_km_per_soc": round(85.0 / 25.0, 3),
        "correction_factor": round(2.5 / (85.0 / 25.0), 3),
        "avg_power_raw": 58.0,
    }
    for i in range(2)
]

check("10 synthetic trips created", len(synthetic_trips) == 10,
      f"{len(synthetic_trips)} trips")

# ---------------------------------------------------------------------------
# Step 2 — Build model
# ---------------------------------------------------------------------------
print("\n=== Step 2: Model build ===")

model, learning_pct, system_state = build_model(synthetic_trips, alpha=0.15, min_trips=5)

profile_a = model.get("winter|cold|city|short|cold_start")
profile_b = model.get("winter|near_zero|mixed|short|cold_start")
profile_c = model.get("spring|cool|highway|long|cold_start")

check("Profile A exists",        profile_a is not None)
check("Profile A count = 5",     profile_a and profile_a["count"] == 5,
      str(profile_a["count"] if profile_a else "missing"))
check("Profile A ready = True",  profile_a and profile_a["ready"] is True)
check("Profile A confidence = high", profile_a and profile_a["confidence"] == "high",
      profile_a["confidence"] if profile_a else "missing")
check("Profile A km_per_soc_ewa > 0",
      profile_a and profile_a["km_per_soc_ewa"] and profile_a["km_per_soc_ewa"] > 0)

check("Profile B exists",        profile_b is not None)
check("Profile B count = 3",     profile_b and profile_b["count"] == 3)
check("Profile B ready = False", profile_b and profile_b["ready"] is False)
check("Profile B confidence = low", profile_b and profile_b["confidence"] == "low")

check("Profile C exists",        profile_c is not None)
check("Profile C count = 2",     profile_c and profile_c["count"] == 2)
check("Profile C confidence = preliminary", profile_c and profile_c["confidence"] == "preliminary")

check("learning_pct is int",     isinstance(learning_pct, int))
check("learning_pct >= 0",       learning_pct >= 0)
check("system_state is string",  isinstance(system_state, str))
check("system_state valid",      system_state in ("collecting", "learning", "partial", "reliable"),
      system_state)

# ---------------------------------------------------------------------------
# Step 3 — Predictor lookup and target SOC
# ---------------------------------------------------------------------------
print("\n=== Step 3: Predictor ===")

# Query that matches Profile A exactly
p, reason = lookup_profile(model, "winter", "cold", "city", "short", preheating=False)
check("Exact profile A lookup succeeds", p is not None)
check("Exact lookup has no fallback",    reason is None, str(reason))

target, fallback_used, _ = compute_target(p, reason, min_soc=20, buffer=5, typical_km=30)
check("Target SOC is int",          isinstance(target, int))
check("Target SOC in range 25-100", 25 <= target <= 100, str(target))
print(f"         Target SOC (profile A, typical_km=30): {target}%")

# Query that needs fallback (no profile for summer|hot|highway|long)
p2, reason2 = lookup_profile(model, "summer", "hot", "highway", "long", preheating=False)
target2, fallback2, reason2_out = compute_target(p2, reason2, min_soc=20, buffer=5)
check("No-profile query returns fallback", fallback2 is True or p2 is None)
check("Fallback target >= min_soc+buffer", target2 >= 25, str(target2))

# ---------------------------------------------------------------------------
# Step 4 — Temp band and season helpers
# ---------------------------------------------------------------------------
print("\n=== Step 4: Helper functions ===")

check("temp -20 -> jaakyma",   get_temp_band(-20) == "jaakyma")
check("temp -10 -> cold",      get_temp_band(-10) == "cold")
check("temp 0   -> near_zero", get_temp_band(0)   == "near_zero")
check("temp 5   -> cool",      get_temp_band(5)   == "cool")
check("temp 12  -> mild",      get_temp_band(12)  == "mild")
check("temp 20  -> normal",    get_temp_band(20)  == "normal")
check("temp 30  -> hot",       get_temp_band(30)  == "hot")
check("month 1  -> winter",    get_season(1)      == "winter")
check("month 4  -> spring",    get_season(4)      == "spring")
check("month 7  -> summer",    get_season(7)      == "summer")
check("month 10 -> autumn",    get_season(10)     == "autumn")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
total   = len(results)
passed  = sum(results)
failed  = total - passed
summary = f"{passed}/{total} checks passed"
if failed:
    print(f"\033[91m=== FAIL: {summary} ===\033[0m")
    sys.exit(1)
else:
    print(f"\033[92m=== PASS: {summary} ===\033[0m")

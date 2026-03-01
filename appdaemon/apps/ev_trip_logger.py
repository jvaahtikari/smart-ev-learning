"""
ev_trip_logger.py — Smart EV Trip Logger
Monitors sensor.smart_motor for engine on/off transitions,
records trip segments and writes them to trips.json.
"""

import json
import os
from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass

# ---------------------------------------------------------------------------
# Entity name constants — update these to match your HA installation
# ---------------------------------------------------------------------------
ENTITY_MOTOR          = "sensor.smart_motor"
ENTITY_BATTERY        = "sensor.smart_battery"
ENTITY_RANGE          = "sensor.smart_range"
ENTITY_ODOMETER       = "sensor.smart_odometer"
ENTITY_PREHEAT        = "sensor.smart_pre_climate_active"
ENTITY_CHARGER        = "sensor.zag063912_charger_mode"
ENTITY_WEATHER        = "weather.forecast_koti"

ENGINE_ON_STATE       = "engine_running"
ENGINE_OFF_STATE      = "engine_off"
STOP_TIMEOUT_SEC      = 300  # 5 minutes — traffic stop tolerance

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


def get_season(month):
    return SEASON_MONTHS.get(month, "winter")


class EVTripLogger(hass.Hass):

    def initialize(self):
        self.log("EVTripLogger starting")
        self.log_file = self.args.get("log_file", "/config/ev_trips/trips.json")
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

        self._segment = None        # current open segment
        self._stop_timer = None     # handle for the 5-min stop timer

        self.listen_state(self._on_motor_change, ENTITY_MOTOR)
        self.log(f"Listening to {ENTITY_MOTOR}")

    # ------------------------------------------------------------------
    # State change handler
    # ------------------------------------------------------------------

    def _on_motor_change(self, entity, attribute, old, new, kwargs):
        if old == new:
            return

        if new == ENGINE_ON_STATE:
            self._engine_on()
        elif new == ENGINE_OFF_STATE:
            self._engine_off()

    def _engine_on(self):
        # Cancel pending stop timer (traffic stop — continue segment)
        if self._stop_timer is not None:
            self.cancel_timer(self._stop_timer)
            self._stop_timer = None
            self.log("Traffic stop cancelled — continuing segment")
            return

        # Start a new segment
        self._segment = self._snapshot("start")
        self.log(f"Segment started: SOC={self._segment['soc_start']}% "
                 f"odo={self._segment['odometer_start']} km")

    def _engine_off(self):
        if self._segment is None:
            return
        self._stop_timer = self.run_in(self._stop_timeout, STOP_TIMEOUT_SEC)
        self.log("Engine off — 5-min stop timer started")

    def _stop_timeout(self, kwargs):
        self._stop_timer = None
        if self._segment is None:
            return
        end = self._snapshot("end")
        self._close_segment(end)

    # ------------------------------------------------------------------
    # Snapshot current sensor values
    # ------------------------------------------------------------------

    def _snapshot(self, label):
        now = datetime.now(timezone.utc)
        temp = self._get_weather_temp()
        preheat = self._is_preheating()
        charger = self.get_state(ENTITY_CHARGER) or "unknown"
        plugged_preheat = preheat and charger.lower() not in ("off", "unavailable", "unknown", "none")

        return {
            f"time_{label}":             now.isoformat(),
            f"soc_{label}":              self._float(ENTITY_BATTERY),
            f"range_estimate_{label}":   self._float(ENTITY_RANGE),
            f"odometer_{label}":         self._float(ENTITY_ODOMETER),
            f"temp_{label}":             temp,
            "preheating":                preheat,
            "plugged_in_preheat":        plugged_preheat,
        }

    def _float(self, entity):
        try:
            return float(self.get_state(entity) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _get_weather_temp(self):
        try:
            attrs = self.get_state(ENTITY_WEATHER, attribute="all") or {}
            return float(attrs.get("attributes", {}).get("temperature", 0))
        except (TypeError, ValueError):
            return 0.0

    def _is_preheating(self):
        state = (self.get_state(ENTITY_PREHEAT) or "off").lower()
        return state not in ("off", "unavailable", "unknown", "false", "0")

    # ------------------------------------------------------------------
    # Close segment and save
    # ------------------------------------------------------------------

    def _close_segment(self, end):
        seg = {**self._segment, **end}
        self._segment = None

        try:
            trip = self._build_trip(seg)
        except Exception as exc:
            self.log(f"Trip build error: {exc}", level="WARNING")
            return

        if trip is None:
            return  # filtered out

        self._append_trip(trip)
        self.set_state("sensor.ev_last_trip_saved", state=datetime.now().isoformat())
        self.log(f"Trip saved: {trip['distance_km']:.1f} km, "
                 f"{trip['consumed_soc']:.1f}% SOC, "
                 f"{trip['drive_type']} / {trip['trip_type']}")

    def _build_trip(self, seg):
        t_start = datetime.fromisoformat(seg["time_start"])
        t_end   = datetime.fromisoformat(seg["time_end"])

        soc_start = seg["soc_start"]
        soc_end   = seg["soc_end"]
        odo_start = seg["odometer_start"]
        odo_end   = seg["odometer_end"]

        distance_km   = odo_end - odo_start
        consumed_soc  = soc_start - soc_end
        duration_min  = (t_end - t_start).total_seconds() / 60

        # Filter invalid segments
        if distance_km < 0.3:
            self.log(f"Discarded: distance {distance_km:.2f} km < 0.3")
            return None
        if duration_min < 1:
            self.log(f"Discarded: duration {duration_min:.1f} min < 1")
            return None
        if consumed_soc <= 0:
            self.log(f"Discarded: consumed_soc={consumed_soc:.1f} <= 0")
            return None

        temp_actual  = (seg["temp_start"] + seg["temp_end"]) / 2
        avg_speed    = (distance_km / duration_min) * 60

        drive_type   = "city" if avg_speed < 50 else ("mixed" if avg_speed < 80 else "highway")
        trip_type    = "short" if distance_km < 20 else "long"
        calc_basis   = "time" if (trip_type == "short" and temp_actual < 5) else "km"

        range_est    = seg.get("range_estimate_start", 0)
        car_km_soc   = (range_est / soc_start) if soc_start > 0 else 0
        actual_km_soc = (distance_km / consumed_soc) if consumed_soc > 0 else 0
        correction   = (car_km_soc / actual_km_soc) if actual_km_soc > 0 else 0

        month        = t_start.month
        season       = get_season(month)
        temp_band    = get_temp_band(temp_actual)
        winter_tyres = (self.get_state("input_boolean.ev_winter_tyres") or "off") == "on"

        return {
            "timestamp":            t_start.strftime("%Y-%m-%dT%H:%M:%S"),
            "season":               season,
            "temp_band":            temp_band,
            "temp_actual":          round(temp_actual, 1),
            "winter_tyres":         winter_tyres,
            "distance_km":          round(distance_km, 2),
            "duration_min":         round(duration_min, 1),
            "trip_type":            trip_type,
            "drive_type":           drive_type,
            "calc_basis":           calc_basis,
            "soc_start":            round(soc_start, 1),
            "soc_end":              round(soc_end, 1),
            "consumed_soc":         round(consumed_soc, 1),
            "preheating":           seg["preheating"],
            "plugged_in_preheat":   seg["plugged_in_preheat"],
            "range_estimate_start": round(range_est, 1),
            "car_km_per_soc":       round(car_km_soc, 2),
            "actual_km_per_soc":    round(actual_km_soc, 2),
            "correction_factor":    round(correction, 3),
        }

    def _append_trip(self, trip):
        trips = []
        if os.path.exists(self.log_file):
            try:
                with open(self.log_file) as f:
                    trips = json.load(f)
            except (json.JSONDecodeError, OSError):
                trips = []
        trips.append(trip)
        with open(self.log_file, "w") as f:
            json.dump(trips, f, indent=2)

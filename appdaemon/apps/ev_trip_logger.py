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
ENTITY_AVG_SPEED      = "sensor.smart_average_speed"     # car's own rolling trip average; resets at engine_on
ENTITY_EXTERIOR_TEMP  = "sensor.smart_exterior_temperature"   # car's outdoor temperature sensor
ENTITY_INTERIOR_TEMP  = "sensor.smart_interior_temperature"   # cabin temperature sensor

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

        self._segment     = None    # current open segment
        self._stop_timer  = None    # handle for the 5-min stop timer
        self._preheat_soc = None    # SOC at moment preheating activated (for cost measurement)

        self.listen_state(self._on_motor_change, ENTITY_MOTOR)
        self.listen_state(self._on_preheat_change, ENTITY_PREHEAT)
        self.log(f"Listening to {ENTITY_MOTOR} and {ENTITY_PREHEAT}")

    # ------------------------------------------------------------------
    # State change handlers
    # ------------------------------------------------------------------

    def _on_motor_change(self, entity, attribute, old, new, kwargs):
        if old == new:
            return
        if new == ENGINE_ON_STATE:
            self._engine_on()
        elif new == ENGINE_OFF_STATE:
            self._engine_off()

    def _on_preheat_change(self, entity, attribute, old, new, kwargs):
        """Snapshot battery SOC the moment preheating activates.
        Used later at engine_on to calculate SOC consumed by unplugged preheating.
        Only capture on the first activation since last trip end (_preheat_soc is None).
        """
        old_active = (old or "off").lower() not in ("off", "unavailable", "unknown", "false", "0")
        new_active = (new or "off").lower() not in ("off", "unavailable", "unknown", "false", "0")
        if not old_active and new_active and self._preheat_soc is None:
            self._preheat_soc = self._float(ENTITY_BATTERY)
            self.log(f"Preheat activated — SOC snapshot: {self._preheat_soc}%")

    def _engine_on(self):
        # Cancel pending stop timer (traffic stop — continue segment)
        if self._stop_timer is not None:
            self.cancel_timer(self._stop_timer)
            self._stop_timer = None
            self.log("Traffic stop cancelled — continuing segment")
            return

        # Start a new segment
        self._segment = self._snapshot("start")

        # Calculate preheat SOC cost — only meaningful when preheated without charger
        charger = self.get_state(ENTITY_CHARGER) or "unknown"
        plugged  = charger.lower() not in ("off", "unavailable", "unknown", "none")
        if self._preheat_soc is not None and not plugged:
            preheat_soc_cost = max(0.0, round(self._preheat_soc - self._segment["soc_start"], 1))
        else:
            preheat_soc_cost = 0.0
        self._segment["preheat_soc_cost"] = preheat_soc_cost
        self._preheat_soc = None  # reset for next trip

        extra = f", preheat_cost={preheat_soc_cost}%" if preheat_soc_cost > 0 else ""
        self.log(f"Segment started: SOC={self._segment['soc_start']}% "
                 f"odo={self._segment['odometer_start']} km{extra}")

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
        now  = datetime.now(timezone.utc)
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
            f"exterior_temp_{label}":    self._get_exterior_temp(),
            f"interior_temp_{label}":    self._float(ENTITY_INTERIOR_TEMP),
            f"avg_speed_{label}":        self._float(ENTITY_AVG_SPEED),
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

    def _get_exterior_temp(self):
        """Car's own exterior temperature sensor — more precise than weather API."""
        try:
            v = self.get_state(ENTITY_EXTERIOR_TEMP)
            return float(v) if v not in (None, "unavailable", "unknown") else 0.0
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

        # Temperature — prefer car's exterior sensor; fall back to weather API
        exterior_temp     = seg.get("exterior_temp_start", 0.0)
        interior_temp_start = seg.get("interior_temp_start", 0.0)
        weather_temp_avg  = (seg["temp_start"] + seg["temp_end"]) / 2
        temp_actual       = exterior_temp if exterior_temp != 0.0 else weather_temp_avg

        # Preheat temperature delta: how much warmer the cabin was vs outside at engine_on.
        # Large delta = cabin was well-heated relative to exterior (plugged or unplugged preheat).
        # preheat_soc_cost: SOC drained from battery by unplugged preheating (0 if plugged in).
        preheat_temp_delta = round(interior_temp_start - exterior_temp, 1) if seg.get("preheating") else 0.0
        preheat_soc_cost   = seg.get("preheat_soc_cost", 0.0)

        # Prefer car's own trip average speed (more accurate than calculated)
        car_avg_speed = seg.get("avg_speed_end", 0)
        avg_speed     = car_avg_speed if car_avg_speed > 0 else (distance_km / duration_min) * 60

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
            "exterior_temp":        round(exterior_temp, 1),
            "interior_temp_start":  round(interior_temp_start, 1),
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
            "preheat_temp_delta":   preheat_temp_delta,
            "preheat_soc_cost":     round(preheat_soc_cost, 1),
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

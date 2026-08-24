"""
Turns a HYDROS state document into Prometheus metrics.

Sensor and output names are user-assigned, so nothing here hardcodes a schema:
the collector walks whatever keys the document contains. Recognised sensor names
get a typed, unit-suffixed metric; anything else still lands as
hydros_input_reading{name,field} so no reading is silently dropped.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

# The spec documents voltageI as millivolts and powerI as milliwatts, but its own
# example contradicts both: a heater at voltageI 12062 is 120.62 V mains, and a
# pump at 23.41 V x 1.177 A is 27.6 W against powerI 275. Both are hundredths and
# tenths respectively. Re-verify against the app before trusting these.
DIVISOR_TEMPERATURE = 10.0
DIVISOR_VOLTAGE = 100.0
DIVISOR_CURRENT = 1000.0
DIVISOR_POWER = 10.0

# valueState runs 0..10000 full scale, and -10000..10000 on reversible pumps.
FULL_SCALE = 10000.0

# Multi-channel outputs flatten their channels into prefixed fields, e.g. AWC
# carries both valueState and drainvalueState.
_CHANNEL_RE = re.compile(r"^(?P<channel>[A-Za-z0-9]+)valueState$")

_READING_FIELDS = ("probeValue", "senseValue", "value")

UNIT_METRICS = {
    "celsius": ("hydros_input_temperature_celsius", "Calibrated temperature in degrees Celsius."),
    "ph": ("hydros_input_ph", "Calibrated pH reading."),
    "ppt": ("hydros_input_salinity_ppt", "Calibrated salinity in parts per thousand."),
    "orp": ("hydros_input_orp_millivolts", "Calibrated ORP reading in millivolts."),
    "dkh": ("hydros_input_alkalinity_dkh", "Calibrated alkalinity in dKH."),
    "level": ("hydros_input_level", "Level sensor state, 1 when satisfied."),
}

# Matched against whole tokens of the lowercased name, so "pH" does not match
# "Phosphate".
_EXACT_TOKENS = {"ph": "ph", "orp": "orp", "level": "level", "ato": "level"}
_PREFIX_TOKENS = (("temp", "celsius"), ("salin", "ppt"), ("sal", "ppt"), ("alk", "dkh"))


def classify_input(name: str, overrides: dict[str, str] | None = None) -> tuple[str, str] | None:
    """Map a sensor name to a (metric_name, help) pair, or None if unrecognised."""
    if overrides:
        forced = overrides.get(name) or overrides.get(name.lower())
        if forced and forced.lower() in UNIT_METRICS:
            return UNIT_METRICS[forced.lower()]

    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    for token in tokens:
        if token in _EXACT_TOKENS:
            return UNIT_METRICS[_EXACT_TOKENS[token]]
    for token in tokens:
        for prefix, unit in _PREFIX_TOKENS:
            if token.startswith(prefix):
                return UNIT_METRICS[unit]
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(value: object) -> float:
    return 1.0 if value else 0.0


def _naive(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def boot_epoch(state: dict, health: dict) -> float | None:
    """
    Derive a boot timestamp without parsing the trailing timezone abbreviation.
    `time` and `bootTime` share a timezone, so their naive difference is a valid
    uptime, which anchors against the millis epoch.
    """
    millis = _number(state.get("millis"))
    now_local = _naive(health.get("time") or state.get("time"))
    booted = _naive(health.get("bootTime") or state.get("bootTime"))
    if millis is None or now_local is None or booted is None:
        return None
    return millis / 1000.0 - (now_local - booted).total_seconds()


@dataclass
class Snapshot:
    state: dict | None = None
    fetched_at: float = 0.0
    last_success: float = 0.0
    session_expires: float = 0.0
    device: dict = field(default_factory=dict)
    metadata: list = field(default_factory=list)


class Cache:
    """Written by the poll loop, read by the scrape thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = Snapshot()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snap

    def _replace(self, **changes) -> None:
        with self._lock:
            current = self._snap
            self._snap = Snapshot(
                state=changes.get("state", current.state),
                fetched_at=changes.get("fetched_at", current.fetched_at),
                last_success=changes.get("last_success", current.last_success),
                session_expires=changes.get("session_expires", current.session_expires),
                device=changes.get("device", current.device),
                metadata=changes.get("metadata", current.metadata),
            )

    def set_state(self, state: dict, now: float) -> None:
        self._replace(state=state, fetched_at=now, last_success=now)

    def clear_state(self) -> None:
        """
        Drops the readings rather than republishing the last known values. A
        stale value re-served every 30s writes fiction into a two-year store;
        an absent series shows an honest gap.
        """
        self._replace(state=None)

    def set_session_expiry(self, expires_at: float) -> None:
        self._replace(session_expires=expires_at)

    def set_device(self, device: dict) -> None:
        self._replace(device=device)

    def set_metadata(self, metadata: list) -> None:
        self._replace(metadata=metadata)


class StateCollector:
    def __init__(
        self,
        cache: Cache,
        input_units: dict[str, str] | None = None,
        stale_after: float = 180.0,
    ) -> None:
        self._cache = cache
        self._input_units = input_units or {}
        self._stale_after = stale_after

    def describe(self):
        return []

    def collect(self):
        snap = self._cache.snapshot()
        now = time.time()
        state = snap.state

        # A single failed poll should not punch a hole in the graph, but data
        # this old is no longer a reading. Past the threshold the series stop
        # rather than repeating a value the tank no longer holds.
        if state is not None and now - snap.fetched_at > self._stale_after:
            state = None

        yield GaugeMetricFamily(
            "hydros_up", "1 when the last state poll succeeded.", value=_flag(state)
        )
        yield GaugeMetricFamily(
            "hydros_last_poll_success_timestamp_seconds",
            "Unix time of the last successful state poll.",
            value=snap.last_success,
        )
        yield GaugeMetricFamily(
            "hydros_session_expires_timestamp_seconds",
            "Unix time the current polling session expires.",
            value=snap.session_expires,
        )
        if state is None:
            return

        yield GaugeMetricFamily(
            "hydros_state_age_seconds",
            "Age of the cached state document.",
            value=max(0.0, now - snap.fetched_at),
        )
        yield from self._collective(state)
        yield from self._inputs(state)
        yield from self._outputs(state)
        yield from self._devices(state, snap.device)

    def _collective(self, state: dict):
        status = _number(state.get("collectiveStatus"))
        if status is not None:
            yield GaugeMetricFamily(
                "hydros_collective_status", "Collective health status code.", value=status
            )
        config = _number(state.get("configNum"))
        if config is not None:
            yield GaugeMetricFamily(
                "hydros_collective_config_revision",
                "Device configuration revision number.",
                value=config,
            )
        reported = _number(state.get("millis"))
        if reported is not None:
            yield GaugeMetricFamily(
                "hydros_collective_report_timestamp_seconds",
                "Device-reported time of the state document.",
                value=reported / 1000.0,
            )
        mode = state.get("mode")
        if isinstance(mode, str) and mode:
            family = GaugeMetricFamily(
                "hydros_collective_mode_info", "Current operating mode.", labels=["mode"]
            )
            family.add_metric([mode], 1.0)
            yield family

    def _inputs(self, state: dict):
        inputs = state.get("Input")
        if not isinstance(inputs, dict):
            return

        typed: dict[str, GaugeMetricFamily] = {}
        raw = GaugeMetricFamily(
            "hydros_input_raw", "Uncalibrated probe reading.", labels=["name"]
        )
        reading = GaugeMetricFamily(
            "hydros_input_reading",
            "Reading from a sensor whose name matched no known type.",
            labels=["name", "field"],
        )
        alert = GaugeMetricFamily(
            "hydros_input_alert_active", "1 when the sensor reports an alert.", labels=["name"]
        )

        for name, entry in inputs.items():
            if not isinstance(entry, dict):
                continue
            alert.add_metric([name], _flag(entry.get("alert")))

            raw_value = _number(entry.get("probeRawValue"))
            if raw_value is not None:
                raw.add_metric([name], raw_value)

            kind = classify_input(name, self._input_units)
            if kind is None:
                for key, value in entry.items():
                    number = _number(value)
                    if number is not None:
                        reading.add_metric([name, key], number)
                continue

            metric_name, helptext = kind
            for candidate in _READING_FIELDS:
                value = _number(entry.get(candidate))
                if value is not None:
                    family = typed.get(metric_name)
                    if family is None:
                        family = GaugeMetricFamily(metric_name, helptext, labels=["name"])
                        typed[metric_name] = family
                    family.add_metric([name], value)
                    break

        yield from typed.values()
        yield raw
        yield reading
        yield alert

    def _outputs(self, state: dict):
        outputs = state.get("Output")
        if not isinstance(outputs, dict):
            return

        labels = ["name", "channel"]
        level = GaugeMetricFamily(
            "hydros_output_level_ratio",
            "Output level as a fraction of full scale; negative when reversed.",
            labels=labels,
        )
        on = GaugeMetricFamily(
            "hydros_output_on", "1 when the output is driving.", labels=labels
        )
        override = GaugeMetricFamily(
            "hydros_output_override_active",
            "1 when an override is holding the output off schedule.",
            labels=labels,
        )
        waiting = GaugeMetricFamily(
            "hydros_output_waiting",
            "1 when the output is in a min/max on/off wait state.",
            labels=labels,
        )
        alert = GaugeMetricFamily(
            "hydros_output_alert_active", "1 when the output reports an alert.", labels=labels
        )
        volts = GaugeMetricFamily(
            "hydros_output_voltage_volts", "Supply voltage.", labels=["name"]
        )
        amps = GaugeMetricFamily(
            "hydros_output_current_amperes", "Current draw.", labels=["name"]
        )
        watts = GaugeMetricFamily("hydros_output_power_watts", "Power draw.", labels=["name"])
        rpm = GaugeMetricFamily("hydros_output_rpm", "Motor speed.", labels=["name"])
        schedule = GaugeMetricFamily(
            "hydros_output_schedule_info",
            "Active schedule for the output.",
            labels=["name", "schedule"],
        )

        for name, entry in outputs.items():
            if not isinstance(entry, dict):
                continue

            for channel in self._channels(entry):
                prefix = channel
                value = _number(entry.get(f"{prefix}valueState"))
                if value is not None:
                    level.add_metric([name, channel], value / FULL_SCALE)
                    on.add_metric([name, channel], 1.0 if value != 0 else 0.0)
                override.add_metric([name, channel], _flag(entry.get(f"{prefix}override")))
                waiting.add_metric([name, channel], _flag(entry.get(f"{prefix}waiting")))
                alert.add_metric([name, channel], _flag(entry.get(f"{prefix}alert")))

            for source, divisor, family in (
                ("voltageI", DIVISOR_VOLTAGE, volts),
                ("current", DIVISOR_CURRENT, amps),
                ("powerI", DIVISOR_POWER, watts),
                ("rpm", 1.0, rpm),
            ):
                value = _number(entry.get(source))
                if value is not None:
                    family.add_metric([name], value / divisor)

            active = entry.get("schedule")
            if isinstance(active, str) and active:
                schedule.add_metric([name, active], 1.0)

        yield from (level, on, override, waiting, alert, volts, amps, watts, rpm, schedule)
        yield from self._output_metadata()

    @staticmethod
    def _channels(entry: dict) -> list[str]:
        channels = [""] if "valueState" in entry else []
        for key in entry:
            match = _CHANNEL_RE.match(key)
            if match:
                channels.append(match.group("channel"))
        return channels

    def _output_metadata(self):
        entries = self._cache.snapshot().metadata
        if not entries:
            return
        info = GaugeMetricFamily(
            "hydros_output_info",
            "Overridable output capabilities reported by the device.",
            labels=["name", "channel", "key", "type"],
        )
        for entry in entries:
            raw_name = str(entry.get("name", ""))
            if not raw_name:
                continue
            # Metadata names a channel "AWC / drain"; the state document splits the
            # same thing into name and a field prefix. Normalise so the two join.
            name, _, channel = raw_name.partition(" / ")
            info.add_metric(
                [name, channel, str(entry.get("key", "")), str(entry.get("type", ""))], 1.0
            )
        yield info

    def _devices(self, state: dict, scoped: dict):
        health = state.get("health")
        ota = state.get("otaState")

        temperature = GaugeMetricFamily(
            "hydros_device_temperature_celsius", "Internal device temperature.", labels=["device"]
        )
        bus_volts = GaugeMetricFamily(
            "hydros_device_bus_voltage_volts", "Accessory bus voltage.", labels=["device"]
        )
        bus_amps = GaugeMetricFamily(
            "hydros_device_bus_current_amperes", "Accessory bus current.", labels=["device"]
        )
        wifi = GaugeMetricFamily(
            "hydros_device_wifi_state", "WiFi connection state code.", labels=["device"]
        )
        sdcard = GaugeMetricFamily(
            "hydros_device_sdcard_status", "SD card status code, 0 when healthy.", labels=["device"]
        )
        booted = GaugeMetricFamily(
            "hydros_device_boot_timestamp_seconds", "Unix time the device booted.",
            labels=["device"],
        )
        runs = CounterMetricFamily(
            "hydros_device_selftest_runs", "Self-tests run since boot.", labels=["device"]
        )
        passes = CounterMetricFamily(
            "hydros_device_selftest_passes", "Self-tests passed since boot.", labels=["device"]
        )

        if isinstance(health, dict):
            for mac, entry in health.items():
                if not isinstance(entry, dict):
                    continue
                value = _number(entry.get("temperatureI"))
                if value is not None:
                    temperature.add_metric([mac], value / DIVISOR_TEMPERATURE)
                bus = entry.get("busPower")
                if isinstance(bus, dict):
                    value = _number(bus.get("voltageI"))
                    if value is not None:
                        bus_volts.add_metric([mac], value / DIVISOR_VOLTAGE)
                    value = _number(bus.get("current"))
                    if value is not None:
                        bus_amps.add_metric([mac], value / DIVISOR_CURRENT)
                for source, family in (("wifiState", wifi), ("sdCardStatus", sdcard)):
                    value = _number(entry.get(source))
                    if value is not None:
                        family.add_metric([mac], value)
                for source, family in (
                    ("selfTestRunCount", runs),
                    ("selfTestPassCount", passes),
                ):
                    value = _number(entry.get(source))
                    if value is not None:
                        family.add_metric([mac], value)
                epoch = boot_epoch(state, entry)
                if epoch is not None:
                    booted.add_metric([mac], epoch)

        yield from (temperature, bus_volts, bus_amps, wifi, sdcard, booted, runs, passes)

        if isinstance(ota, dict):
            state_family = GaugeMetricFamily(
                "hydros_device_ota_state", "Firmware update state code.", labels=["device"]
            )
            running = GaugeMetricFamily(
                "hydros_device_firmware_running", "Running firmware version.", labels=["device"]
            )
            for mac, entry in ota.items():
                if not isinstance(entry, dict):
                    continue
                for source, family in (
                    ("otaState", state_family),
                    ("runningVer", running),
                ):
                    value = _number(entry.get(source))
                    if value is not None:
                        family.add_metric([mac], value)
            yield from (state_family, running)

        if scoped.get("deviceId"):
            info = GaugeMetricFamily(
                "hydros_device_info",
                "The device the API key is scoped to.",
                labels=["device", "friendly_name", "type"],
            )
            info.add_metric(
                [
                    str(scoped.get("deviceId", "")),
                    str(scoped.get("friendlyName", "")),
                    str(scoped.get("type", "")),
                ],
                1.0,
            )
            yield info

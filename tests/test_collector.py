import json
import time
from pathlib import Path

import pytest

from hydros.collector import Cache, StateCollector, classify_input, uptime_seconds
from hydros.__main__ import parse_input_units

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sampled():
    cache = Cache()
    cache.set_state(load("state.json"), time.time())
    cache.set_device(load("device.json"))
    cache.set_metadata(load("metadata.json"))
    collector = StateCollector(cache)
    out = {}
    for family in collector.collect():
        for sample in family.samples:
            out[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return out


def value(sampled, metric, **labels):
    return sampled[(metric, tuple(sorted(labels.items())))]


def test_up_and_freshness(sampled):
    assert value(sampled, "hydros_up") == 1.0
    assert value(sampled, "hydros_state_age_seconds") < 5


def test_probe_readings_are_passed_through(sampled):
    assert value(sampled, "hydros_input_ph", name="pH") == pytest.approx(8.012)
    assert value(sampled, "hydros_input_salinity_ppt", name="Salinity") == pytest.approx(36.148)
    assert value(sampled, "hydros_input_orp_millivolts", name="ORP") == pytest.approx(177.879)
    assert value(sampled, "hydros_input_raw", name="pH") == pytest.approx(-308)


def test_temperature_inputs_use_sense_value(sampled):
    assert value(
        sampled, "hydros_input_temperature_celsius", name="Temperature 1"
    ) == pytest.approx(25.875)
    assert value(
        sampled, "hydros_input_temperature_celsius", name="Temperature 2"
    ) == pytest.approx(25.7675)


def test_every_celsius_family_has_a_fahrenheit_twin(sampled):
    assert value(
        sampled, "hydros_input_temperature_fahrenheit", name="Temperature 1"
    ) == pytest.approx(78.575)
    assert value(
        sampled, "hydros_device_temperature_fahrenheit", device="d0ef766b2090"
    ) == pytest.approx(81.5)

    celsius = {m for m, _ in sampled if m.endswith("_temperature_celsius")}
    fahrenheit = {m for m, _ in sampled if m.endswith("_temperature_fahrenheit")}
    assert {m.replace("_celsius", "_fahrenheit") for m in celsius} == fahrenheit


def test_fahrenheit_twin_carries_the_same_labels(sampled):
    def labels_for(suffix):
        return {lbls for m, lbls in sampled if m == f"hydros_input_temperature_{suffix}"}

    assert labels_for("celsius") == labels_for("fahrenheit")


def test_alkalinity_reads_plain_value_and_flags_its_alert(sampled):
    assert value(sampled, "hydros_input_alkalinity_dkh", name="Alky Alkalinity") == pytest.approx(
        7.236
    )
    assert value(sampled, "hydros_input_alert_active", name="Alky Alkalinity") == 1.0
    assert value(sampled, "hydros_input_alert_active", name="pH") == 0.0


def test_ato_level_classifies_as_a_level(sampled):
    assert value(sampled, "hydros_input_level", name="ATO Level") == 1.0


def test_device_temperature_is_tenths_of_a_degree(sampled):
    assert value(
        sampled, "hydros_device_temperature_celsius", device="d0ef766b2090"
    ) == pytest.approx(27.5)


def test_mains_voltage_is_hundredths_not_millivolts(sampled):
    # A heater on US mains, which is the check that catches the spec's wrong unit.
    assert value(sampled, "hydros_output_voltage_volts", name="Heaters") == pytest.approx(120.62)


def test_pump_power_reconciles_with_volts_times_amps(sampled):
    volts = value(sampled, "hydros_output_voltage_volts", name="Return Left")
    amps = value(sampled, "hydros_output_current_amperes", name="Return Left")
    watts = value(sampled, "hydros_output_power_watts", name="Return Left")
    assert volts == pytest.approx(23.41)
    assert amps == pytest.approx(1.177)
    assert watts == pytest.approx(27.5)
    assert watts == pytest.approx(volts * amps, rel=0.02)


def test_output_level_is_a_fraction_of_full_scale(sampled):
    assert value(sampled, "hydros_output_level_ratio", name="Return Left", channel="") == 0.75
    assert value(sampled, "hydros_output_on", name="Return Left", channel="") == 1.0
    assert value(sampled, "hydros_output_on", name="Left Octo", channel="") == 0.0
    assert value(sampled, "hydros_output_override_active", name="Left Octo", channel="") == 1.0


def test_multi_channel_outputs_split_into_channels(sampled):
    for channel in ("drain", "fill"):
        assert value(sampled, "hydros_output_level_ratio", name="AWC", channel=channel) == 0.0
        assert value(sampled, "hydros_output_override_active", name="AWC", channel=channel) == 1.0
    assert value(sampled, "hydros_output_alert_active", name="AWC", channel="") == 1.0


def test_schedule_and_mode_render_as_info_series(sampled):
    assert value(sampled, "hydros_collective_mode_info", mode="Normal") == 1.0
    assert value(sampled, "hydros_output_schedule_info", name="Left Octo", schedule="Day") == 1.0


def test_metadata_supplies_output_capabilities(sampled):
    assert (
        value(
            sampled,
            "hydros_output_info",
            name="Return Left",
            channel="",
            key="7f3a2b1c-4d5e-4f6a-8b9c-0d1e2f3a4b5c",
            type="level",
        )
        == 1.0
    )


def test_metadata_channel_labels_join_with_state_metrics(sampled):
    # "AWC / drain" in metadata is name="AWC", channel="drain" in the state doc.
    assert (
        value(
            sampled,
            "hydros_output_info",
            name="AWC",
            channel="drain",
            key="6f7a8b9c-0d1e-4f2a-3b4c-5d6e7f8a9b0c",
            type="bool",
        )
        == 1.0
    )
    assert value(sampled, "hydros_output_level_ratio", name="AWC", channel="drain") == 0.0


def test_health_and_ota_are_labelled_by_mac(sampled):
    mac = "d0ef766b2090"
    assert value(sampled, "hydros_device_bus_voltage_volts", device=mac) == pytest.approx(23.96)
    assert value(sampled, "hydros_device_bus_current_amperes", device=mac) == pytest.approx(0.077)
    assert value(sampled, "hydros_device_wifi_state", device=mac) == 4.0
    assert value(sampled, "hydros_device_selftest_runs_total", device=mac) == 20.0
    assert value(sampled, "hydros_device_firmware_running", device=mac) == 380.0
    assert value(sampled, "hydros_device_uptime_seconds", device=mac) == pytest.approx(1110769.0)


def test_device_info_omits_the_owner_email(sampled):
    key = ("hydros_device_info", (("device", "a0b765227294"), ("friendly_name",
           "Display Tank Controller"), ("type", "X4")))
    assert sampled[key] == 1.0
    assert not any("owner" in dict(labels) for _, labels in sampled)


def test_uptime_is_derived_without_parsing_the_zone():
    state = load("state.json")
    health = state["health"]["d0ef766b2090"]
    # 2026-03-30 16:41:09 to 2026-04-12 13:13:58 in the same zone.
    assert uptime_seconds(state, health) == pytest.approx(1110769.0)


def test_uptime_ignores_the_millis_epoch():
    """A live Quatro-398 counts millis from 2020-01-01, not the Unix epoch."""
    state = {
        "millis": 1577837398401,
        "time": "2026-08-26 21:53:37 EDT",
        "bootTime": "2026-08-26 21:43:38 EDT",
    }
    assert uptime_seconds(state, {}) == pytest.approx(599.0)


def test_no_device_reported_timestamp_is_published(sampled):
    # millis is not a wall clock, so it must not surface as one.
    assert not any(m.startswith("hydros_collective_report") for m, _ in sampled)
    assert not any(m.startswith("hydros_device_boot") for m, _ in sampled)


def test_stale_state_stops_reporting_readings():
    cache = Cache()
    cache.set_state(load("state.json"), time.time() - 10_000)
    names = {s.name for f in StateCollector(cache).collect() for s in f.samples}
    assert "hydros_input_ph" not in names
    assert "hydros_up" in names


def test_offline_device_reports_down():
    cache = Cache()
    cache.set_state(load("state.json"), time.time())
    cache.clear_state()
    collected = {
        s.name: s.value for f in StateCollector(cache).collect() for s in f.samples
    }
    assert collected["hydros_up"] == 0.0
    assert "hydros_input_ph" not in collected


def test_unrecognised_sensors_still_report_their_numbers():
    cache = Cache()
    cache.set_state({"Input": {"Reactor CO2": {"senseValue": 4.5, "value": 2}}}, time.time())
    sampled = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for f in StateCollector(cache).collect()
        for s in f.samples
    }
    assert value(sampled, "hydros_input_reading", name="Reactor CO2", field="senseValue") == 4.5
    assert value(sampled, "hydros_input_reading", name="Reactor CO2", field="value") == 2.0


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pH", "hydros_input_ph"),
        ("Temperature 1", "hydros_input_temperature_celsius"),
        ("Sump Temp", "hydros_input_temperature_celsius"),
        ("Salinity", "hydros_input_salinity_ppt"),
        ("ORP", "hydros_input_orp_millivolts"),
        ("Alky Alkalinity", "hydros_input_alkalinity_dkh"),
        ("ATO Level", "hydros_input_level"),
        ("ATO Water Level", "hydros_input_level"),
        ("Skimmer Overflow", "hydros_input_level"),
        ("Sump Leak", "hydros_input_level"),
        ("pH Probe", "hydros_input_ph"),
    ],
)
def test_known_sensor_names_classify(name, expected):
    assert classify_input(name)[0] == expected


@pytest.mark.parametrize("name", ["Phosphate", "Alpha Probe", "Sulphur Reactor", "Flow"])
def test_unrelated_names_do_not_false_match(name):
    assert classify_input(name) is None


def test_input_units_override_the_heuristic():
    units = parse_input_units("Reactor CO2=ph, Odd One=celsius")
    assert units == {"Reactor CO2": "ph", "Odd One": "celsius"}
    assert classify_input("Reactor CO2", units)[0] == "hydros_input_ph"

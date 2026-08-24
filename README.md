# hydros-exporter

Prometheus exporter for the [CoralVue HYDROS Public API](https://developers.api.coralvuehydros.com/).

Exposes water chemistry, output state and per-device health for a HYDROS
collective. One device key covers the whole collective.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `HYDROS_PROVIDER_KEY` | required | Issued by CoralVue |
| `HYDROS_DEVICE_KEY` | required | Issued in the HYDROS app, read scope is enough |
| `HYDROS_POLL_INTERVAL` | `30` | Seconds; the API's floor is 30 |
| `HYDROS_METADATA_INTERVAL` | `3600` | Override-metadata refresh, seconds |
| `HYDROS_INPUT_UNITS` | — | `Sump Probe=celsius,Reactor=ph` for sensors the name heuristic misses |
| `HYDROS_PORT` | `8080` | |
| `HYDROS_BEARER_AUTH` | `false` | Forces the `Bearer` header form; autodetected otherwise |
| `HYDROS_LOG_LEVEL` | `INFO` | |

Recognised sensor units: `celsius`, `ph`, `ppt`, `orp`, `dkh`, `level`.
Unrecognised sensors are still exported as `hydros_input_reading{name,field}`.

## Running

```sh
pip install -r requirements-dev.txt
pytest

# no API key needed
python -m hydros --mock tests/fixtures/state.json
curl -s localhost:8080/metrics | grep '^hydros_'
```

## Notes

The API is not a plain GET. `POST /device/state/session` mints a 6-hour poll
token and wakes the device; the returned `pollUrl` is then polled with that
token. The exporter renews 30 minutes before expiry and polls on its own
schedule, so API load stays constant regardless of how many Prometheus replicas
scrape it. Session starts are capped at 5/hour per device.

Readings stop being exported once the cached state is older than
`max(3 x poll interval, 180s)` rather than repeating a stale value.

`voltageI` and `powerI` are scaled by 100 and 10 respectively, which
contradicts the spec's stated millivolts/milliwatts but matches its own worked
example. Verify against the app before trusting the derived wattage.

import json
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from hydros.__main__ import build_handler
from hydros.collector import Cache

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def server():
    cache = Cache()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(cache))
    httpd.daemon_threads = True
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cache
    httpd.shutdown()


def get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def test_metrics_is_served(server):
    base, _ = server
    status, ctype, body = get(f"{base}/metrics")
    assert status == 200
    assert "text/plain" in ctype
    assert b"python_info" in body


def test_state_reports_503_until_a_poll_lands(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{base}/state")
    assert exc.value.code == 503


def test_state_returns_the_document_verbatim(server):
    base, cache = server
    state = json.loads((FIXTURES / "state.json").read_text())
    cache.set_state(state, time.time())

    status, ctype, body = get(f"{base}/state")
    assert status == 200
    assert ctype == "application/json"
    payload = json.loads(body)
    assert payload["state"] == state
    assert payload["age_seconds"] < 5


def test_state_exposes_fields_no_metric_derives_from(server):
    """The point of the endpoint: see what is pulled, not just what is exported."""
    base, cache = server
    cache.set_state({"Input": {}, "hostname": "reef", "someUnknownKey": 1}, time.time())
    payload = json.loads(get(f"{base}/state")[2])
    assert payload["state"]["someUnknownKey"] == 1


def test_state_omits_the_device_document(server):
    base, cache = server
    cache.set_state({"Input": {}}, time.time())
    cache.set_device(json.loads((FIXTURES / "device.json").read_text()))
    body = get(f"{base}/state")[2]
    assert b"@" not in body


def test_unknown_path_is_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{base}/nope")
    assert exc.value.code == 404


def test_index_lists_the_routes(server):
    base, _ = server
    body = get(f"{base}/")[2]
    assert b"/metrics" in body and b"/state" in body

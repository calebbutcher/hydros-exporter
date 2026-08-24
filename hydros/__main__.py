"""Maintains the polling session, polls device state, serves /metrics."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from prometheus_client import REGISTRY, start_http_server

from .client import AuthError, HydrosClient, OfflineError, TransientError
from .collector import Cache, StateCollector
from .metrics import POLLS, REQUEST_DURATION, SESSIONS

log = logging.getLogger("hydros")

# Session starts are capped at 5/hour per device, so a rejected key backs off
# well clear of the limit rather than burning the budget on retries.
AUTH_RETRY_SECONDS = 900
BACKOFF_START = 5.0
BACKOFF_CAP = 300.0


def parse_input_units(raw: str) -> dict[str, str]:
    """`Sump Probe=celsius,Reactor=ph` -> {"Sump Probe": "celsius", ...}"""
    units = {}
    for pair in raw.split(","):
        name, sep, unit = pair.partition("=")
        if sep and name.strip() and unit.strip():
            units[name.strip()] = unit.strip().lower()
    return units


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        log.warning("%s is not an integer, using %s", name, default)
        return default


async def _refresh_extras(client: HydrosClient, cache: Cache, want_device: bool) -> bool:
    """Override metadata and the device record change rarely; failure is non-fatal."""
    if want_device:
        try:
            with REQUEST_DURATION.labels("device").time():
                cache.set_device(await client.get_device())
            want_device = False
        except (AuthError, TransientError) as exc:
            log.warning("device lookup failed: %s", exc)
    try:
        with REQUEST_DURATION.labels("metadata").time():
            cache.set_metadata(await client.get_override_metadata())
    except (AuthError, TransientError) as exc:
        log.warning("override metadata refresh failed: %s", exc)
    return want_device


async def run(
    client: HydrosClient, cache: Cache, poll_interval: int, metadata_interval: int
) -> None:
    session = None
    backoff = BACKOFF_START
    next_extras = 0.0
    want_device = True

    while True:
        now = time.time()
        if session is None or now >= session.renew_at:
            try:
                with REQUEST_DURATION.labels("session").time():
                    session = await client.start_session(now)
            except AuthError as exc:
                SESSIONS.labels("unauthorized").inc()
                log.error("session start rejected; check the provider and device keys: %s", exc)
                session = None
                await asyncio.sleep(AUTH_RETRY_SECONDS)
                continue
            except TransientError as exc:
                SESSIONS.labels("error").inc()
                log.warning("session start failed: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
                continue

            SESSIONS.labels("success").inc()
            cache.set_session_expiry(session.expires_at)
            backoff = BACKOFF_START
            poll_interval = max(poll_interval, session.poll_interval)
            log.info(
                "session valid until %s, polling every %ss",
                datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat(),
                poll_interval,
            )

        if now >= next_extras:
            want_device = await _refresh_extras(client, cache, want_device)
            next_extras = now + metadata_interval

        try:
            with REQUEST_DURATION.labels("state").time():
                state = await client.poll_state(session)
        except OfflineError:
            POLLS.labels("offline").inc()
            cache.clear_state()
            log.warning("no cached state for the device; it is probably offline")
        except AuthError as exc:
            POLLS.labels("unauthorized").inc()
            log.warning("poll token rejected, re-minting the session: %s", exc)
            session = None
        except TransientError as exc:
            POLLS.labels("error").inc()
            log.warning("state poll failed: %s", exc)
        else:
            POLLS.labels("success").inc()
            cache.set_state(state, time.time())

        await asyncio.sleep(poll_interval)


async def run_mock(cache: Cache, path: Path, poll_interval: int) -> None:
    state = json.loads(path.read_text())
    for name, setter in (("device", cache.set_device), ("metadata", cache.set_metadata)):
        sibling = path.with_name(f"{name}.json")
        if sibling.exists():
            setter(json.loads(sibling.read_text()))
    log.info("mock mode: serving %s", path)
    while True:
        cache.set_state(state, time.time())
        cache.set_session_expiry(time.time() + 21600)
        await asyncio.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydros", description=__doc__)
    parser.add_argument("--mock", type=Path, help="serve a saved state document instead of polling")
    parser.add_argument("--port", type=int, default=_env_int("HYDROS_PORT", 8080))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("HYDROS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    poll_interval = _env_int("HYDROS_POLL_INTERVAL", 30)
    metadata_interval = _env_int("HYDROS_METADATA_INTERVAL", 3600)
    cache = Cache()
    REGISTRY.register(
        StateCollector(
            cache,
            input_units=parse_input_units(os.environ.get("HYDROS_INPUT_UNITS", "")),
            stale_after=max(3 * poll_interval, 180),
        )
    )
    start_http_server(args.port)
    log.info("serving metrics on :%s", args.port)

    if args.mock:
        return _serve(run_mock(cache, args.mock, poll_interval))

    try:
        provider_key = os.environ["HYDROS_PROVIDER_KEY"]
        device_key = os.environ["HYDROS_DEVICE_KEY"]
    except KeyError as exc:
        parser.error(f"{exc.args[0]} is required (or use --mock)")

    client = HydrosClient(
        provider_key,
        device_key,
        bearer_auth=os.environ.get("HYDROS_BEARER_AUTH", "").lower() in ("1", "true", "yes"),
    )
    return _serve(run(client, cache, poll_interval, metadata_interval), client)


def _serve(coro, client: HydrosClient | None = None) -> int:
    async def supervised():
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
        task = asyncio.create_task(coro)
        done, _ = await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
        else:
            task.cancel()
        if client is not None:
            await client.aclose()

    try:
        asyncio.run(supervised())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

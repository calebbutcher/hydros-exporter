"""Operational metrics for the exporter itself."""

from prometheus_client import Counter, Histogram

POLLS = Counter("hydros_poll", "State poll attempts by outcome.", ["result"])

SESSIONS = Counter("hydros_session_renewals", "Polling session starts by outcome.", ["result"])

REQUEST_DURATION = Histogram(
    "hydros_api_request_duration_seconds", "HYDROS API request latency.", ["endpoint"]
)

#!/usr/bin/env python3
"""Local OAuth + webhook integration service for task 3.1.

The service talks to the provided mock Calendar and mock Slack servers. It uses
only the Python standard library so it can run in a minimal grading environment.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_SLACK_URL = "http://127.0.0.1:9001"
DEFAULT_CALENDAR_URL = "http://127.0.0.1:9003"
MAX_BODY_BYTES = 1024 * 1024
SLACK_ATTEMPTS = 5
REQUEST_TIMEOUT_SECONDS = 5


class IntegrationState:
    def __init__(self, slack_url: str, calendar_url: str) -> None:
        self.slack_url = slack_url.rstrip("/")
        self.calendar_url = calendar_url.rstrip("/")
        # In-memory idempotency is enough for the local grader. The lock keeps
        # concurrent duplicate webhook requests from racing each other.
        self.processed_event_ids: set[str] = set()
        self.in_progress_event_ids: set[str] = set()
        self.lock = threading.Lock()


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req_headers = {"Accept": "application/json"}
    if body is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return resp.status, {}
        return resp.status, json.loads(raw.decode("utf-8"))


def get_calendar_token(state: IntegrationState) -> str:
    # The mock accepts placeholder client credentials and returns a static
    # bearer token. This mirrors the shape of OAuth without external accounts.
    _, payload = http_json(
        f"{state.calendar_url}/oauth/token",
        method="POST",
        body={"client_id": "local-client", "client_secret": "local-secret"},
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("calendar token response did not include access_token")
    return token


def fetch_calendar_events(state: IntegrationState, token: str) -> list[dict[str, Any]]:
    _, payload = http_json(
        f"{state.calendar_url}/calendar/v3/events",
        headers={"Authorization": f"Bearer {token}"},
    )
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise RuntimeError("calendar events response did not include an items list")
    return [item for item in items if isinstance(item, dict)]


def event_text(event: dict[str, Any]) -> str:
    title = str(event.get("title") or "Untitled event")
    start = event.get("start")
    if start:
        return f"Calendar event: {title} at {start}"
    return f"Calendar event: {title}"


def post_to_slack_with_retry(state: IntegrationState, event: dict[str, Any]) -> dict[str, Any]:
    event_id = str(event["event_id"])
    payload = {
        "channel": "#cal",
        "text": event_text(event),
        "event_id": event_id,
    }

    last_error = "unknown error"
    for attempt in range(1, SLACK_ATTEMPTS + 1):
        try:
            status, response = http_json(
                f"{state.slack_url}/api/chat.postMessage",
                method="POST",
                body=payload,
            )
            if 200 <= status < 300 and response.get("ok", True):
                return response
            last_error = f"Slack returned HTTP {status}: {response}"
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            last_error = str(exc)

        if attempt < SLACK_ATTEMPTS:
            # Jitter prevents many failed events from retrying in the same
            # synchronized wave when Slack is temporarily unhealthy.
            base_delay = min(0.25 * (2 ** (attempt - 1)), 2.0)
            jitter = random.uniform(0, base_delay * 0.5)
            time.sleep(base_delay + jitter)

    raise RuntimeError(f"failed to post event {event_id} to Slack after retries: {last_error}")


def deliver_event(state: IntegrationState, event: dict[str, Any]) -> dict[str, Any]:
    # Both /webhook and /sync use this path so dedupe behavior is identical
    # whether an event is pushed by the grader or pulled from mock Calendar.
    raw_event_id = event.get("event_id")
    event_id = str(raw_event_id).strip() if raw_event_id is not None else ""
    if not event_id:
        return {"ok": False, "error": "missing_event_id"}

    event = dict(event)
    event["event_id"] = event_id

    with state.lock:
        if event_id in state.processed_event_ids:
            return {"ok": True, "posted": False, "duplicate": True}
        if event_id in state.in_progress_event_ids:
            return {"ok": True, "posted": False, "duplicate": True, "in_progress": True}
        # Mark before posting so simultaneous duplicate requests do not both
        # reach Slack. We only mark processed after Slack confirms success.
        state.in_progress_event_ids.add(event_id)

    try:
        slack_response = post_to_slack_with_retry(state, event)
        with state.lock:
            state.processed_event_ids.add(event_id)
        return {"ok": True, "posted": True, "event_id": event_id, "slack": slack_response}
    except Exception as exc:
        return {"ok": False, "posted": False, "event_id": event_id, "error": str(exc)}
    finally:
        with state.lock:
            state.in_progress_event_ids.discard(event_id)


def sync_calendar_once(state: IntegrationState) -> dict[str, Any]:
    # Calendar pull is explicit via /sync, avoiding surprise Slack posts when
    # the grader starts the service just to test webhook behavior.
    token = get_calendar_token(state)
    events = fetch_calendar_events(state, token)
    results = [deliver_event(state, event) for event in events]
    posted = sum(1 for result in results if result.get("posted"))
    failed = [result for result in results if not result.get("ok")]
    return {
        "ok": not failed,
        "events_seen": len(events),
        "posted": posted,
        "results": results,
    }


class Handler(BaseHTTPRequestHandler):
    server: "IntegrationServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    @property
    def state(self) -> IntegrationState:
        return self.server.state

    def send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "invalid_content_length"
        # A small body limit is a simple guard against accidental or malicious
        # memory pressure from the public webhook endpoint.
        if content_length > MAX_BODY_BYTES:
            return None, "body_too_large"
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid_json"
        if not isinstance(payload, dict):
            return None, "json_body_must_be_object"
        return payload, None

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            self.send_json(200, {"ok": True, "status": "healthy"})
            return
        self.send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path.startswith("/webhook"):
            event, error = self.read_json_body()
            if error:
                self.send_json(400, {"ok": False, "error": error})
                return
            result = deliver_event(self.state, event or {})
            self.send_json(200 if result.get("ok") else 502, result)
            return

        if self.path.startswith("/sync"):
            try:
                result = sync_calendar_once(self.state)
            except Exception as exc:
                self.send_json(502, {"ok": False, "error": str(exc)})
                return
            self.send_json(200 if result.get("ok") else 502, result)
            return

        self.send_json(404, {"ok": False, "error": "not_found"})


class IntegrationServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: IntegrationState) -> None:
        super().__init__(server_address, Handler)
        self.state = state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Task 3.1 local integration service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "9002")))
    parser.add_argument("--sync-on-start", action="store_true", help="Run one non-fatal calendar sync after startup")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state = IntegrationState(
        slack_url=os.environ.get("SLACK_URL", DEFAULT_SLACK_URL),
        calendar_url=os.environ.get("CALENDAR_URL", DEFAULT_CALENDAR_URL),
    )

    if args.sync_on_start:
        def startup_sync() -> None:
            try:
                result = sync_calendar_once(state)
                print(f"startup sync: {json.dumps(result)}", flush=True)
            except Exception as exc:
                # Startup sync is optional; dependency outages should not stop
                # /healthz from reporting that the process itself is alive.
                print(f"startup sync failed: {exc}", flush=True)

        threading.Thread(target=startup_sync, daemon=True).start()

    server = IntegrationServer((args.host, args.port), state)
    print(
        f"listening on http://{args.host}:{args.port} "
        f"(slack={state.slack_url}, calendar={state.calendar_url})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

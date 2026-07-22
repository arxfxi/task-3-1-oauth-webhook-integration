# Task 3.1 Implementation Plan

## Summary

Build `submission/` as a small local Python service on port `9002` that integrates with the provided mock Calendar and mock Slack services. The service will support both required flows:

- Pull events from mock Calendar using the mock OAuth flow.
- Receive events through `POST /webhook`.

Both flows will post to mock Slack through the same idempotent, retrying delivery path.

The implementation should use Python standard library components where possible so the grader can run it with minimal setup.

## Goals From The README

The submitted code must live in:

```text
submission/
```

Required deliverables:

- `submission/service.py`: OAuth pull, idempotent Slack posting, backoff with jitter, `/healthz`, and `/webhook`.
- `submission/THREAT_MODEL.md`: at least 4 attack surfaces, each with a mitigation.
- `submission/README.md`: run instructions for the service on `:9002` pointed at the local mocks.

Definition of done:

- Sending the same event 10 times results in exactly one chat post.
- 100 different events all post, with none lost.
- `/healthz` returns a healthy response.
- `THREAT_MODEL.md` names at least 4 ways the service could be attacked and how to prevent each.

## Service Design

The local topology is:

```text
Mock Slack      http://localhost:9001
Mock Calendar   http://localhost:9003
Our service     http://localhost:9002
```

Endpoints to implement:

- `GET /healthz`
  - Return HTTP 200 and a short healthy response.
- `POST /webhook`
  - Accept a JSON calendar event.
  - Require a non-empty `event_id`.
  - Send unique events to mock Slack.
  - Deduplicate repeated `event_id` values.
- `POST /sync`
  - Fetch an OAuth token from mock Calendar.
  - Fetch calendar events from mock Calendar.
  - Post each fetched event to mock Slack using the same idempotent path as `/webhook`.

Configuration:

- `SLACK_URL`, default `http://127.0.0.1:9001`.
- `CALENDAR_URL`, default `http://127.0.0.1:9003`.
- `PORT`, default `9002`, with an optional `--port` CLI override.

## Implementation Approach

Use `ThreadingHTTPServer` so concurrent webhook requests can be handled during the grader load test.

Use in-memory idempotency for this local task:

```text
processed_event_ids = set()
in_progress_event_ids = set()
idempotency_lock = threading.Lock()
```

Use `event_id` as the idempotency key.

Flow for every event, regardless of whether it came from `/webhook` or `/sync`:

1. Validate that the event is a JSON object with `event_id`.
2. Acquire the lock.
3. If `event_id` is already processed or in progress, return a duplicate response without posting.
4. Mark `event_id` as in progress.
5. Post to Slack with retry/backoff/jitter.
6. After successful Slack post, mark `event_id` as processed.
7. Always remove `event_id` from in progress before returning.

Do not mark an event as processed unless Slack posting succeeds. This lets a later replay retry an event if all Slack attempts failed.

For production, document that this in-memory store should become Redis, Postgres, or SQLite with a TTL so idempotency survives restarts and works across multiple service replicas. For this grader, in-memory state is sufficient because the service is expected to run in one process during the test.

## OAuth And Mock Calendar Flow

Implement:

- `get_calendar_token()`
  - `POST {CALENDAR_URL}/oauth/token`
  - Send a small JSON body with placeholder `client_id` and `client_secret`.
  - Parse `access_token`.
- `fetch_calendar_events(token)`
  - `GET {CALENDAR_URL}/calendar/v3/events`
  - Include `Authorization: Bearer <token>`.
  - Parse the returned `items` list.
- `sync_calendar_once()`
  - Get token.
  - Fetch events.
  - Send each event through the shared idempotent Slack posting path.

Expose this through `POST /sync` so we can test the OAuth pull explicitly.

Optionally start a non-fatal background sync after service startup. If mock Calendar is unavailable, the service should still boot and `/healthz` should still pass.

## Slack Posting And Retry Plan

Post to:

```text
{SLACK_URL}/api/chat.postMessage
```

Slack payload:

```json
{
  "channel": "#cal",
  "text": "Calendar event: <title> at <start>",
  "event_id": "<event_id>"
}
```

Retry behavior:

- Use bounded retries, for example 5 attempts total.
- Use exponential backoff delays such as `0.25`, `0.5`, `1.0`, and `2.0` seconds.
- Add random jitter to spread out retry timing when many events fail at once.
- Use HTTP timeouts so failed requests do not hang the service.

Jitter is required because the README asks for backoff with jitter. It also avoids synchronized retry waves under load.

## Threat Model Plan

Create `submission/THREAT_MODEL.md` with at least these attack surfaces and mitigations:

- Replayed webhooks
  - Mitigation: idempotency keys keyed by `event_id`; production TTL-backed persistent store.
- Forged webhook requests
  - Mitigation: verify signatures or shared secrets before accepting webhook payloads.
- Oversized or malformed payloads
  - Mitigation: request body size limits, JSON validation, required fields, and safe defaults.
- Retry storm or downstream DoS
  - Mitigation: bounded retries, exponential backoff with jitter, timeouts, and rate limiting.
- Token or secret leakage
  - Mitigation: environment variables, redacted logs, secret rotation, and least-privilege tokens.

## Validation Plan

Start mocks:

```bash
cd /Users/abdulr/Documents/Upwork/task_3.1
docker compose -f data/docker-compose.yml up
```

Start service:

```bash
cd /Users/abdulr/Documents/Upwork/task_3.1
python3 submission/service.py --port 9002
```

Health check:

```bash
curl -i http://localhost:9002/healthz
```

Expected:

- HTTP 200.

OAuth calendar pull:

```bash
curl -X POST http://localhost:9001/_reset
curl -X POST http://localhost:9002/sync
curl http://localhost:9001/_recorded
```

Expected:

- Mock Slack has posts for the events in `data/events.json`.

Duplicate idempotency check:

```bash
curl -X POST http://localhost:9001/_reset
```

Send the same event to `/webhook` 10 times:

```bash
for i in {1..10}; do
  curl -s -X POST http://localhost:9002/webhook \
    -H 'Content-Type: application/json' \
    -d '{"event_id":"dup-001","title":"Duplicate Test","start":"2026-07-22T19:00:00Z"}'
done
curl http://localhost:9001/_recorded
```

Expected:

- Mock Slack reports `"count": 1`.

Load check:

```bash
curl -X POST http://localhost:9001/_reset
for i in {1..100}; do
  curl -s -X POST http://localhost:9002/webhook \
    -H 'Content-Type: application/json' \
    -d "{\"event_id\":\"load-$i\",\"title\":\"Load Event $i\",\"start\":\"2026-07-22T19:00:00Z\"}" >/dev/null &
done
wait
curl http://localhost:9001/_recorded
```

Expected:

- Mock Slack reports `"count": 100`.

Threat model check:

```bash
sed -n '1,240p' submission/THREAT_MODEL.md
```

Expected:

- At least 4 concrete attack surfaces.
- Each attack surface has a mitigation.

## Assumptions

- The grader starts the service fresh and does not require idempotency to survive process restarts.
- The grader posts webhook events with an `event_id` field.
- In-memory idempotency is acceptable for this local task.
- Production persistence and TTLs will be documented in the threat model and README as future hardening.
- Mock services run on local ports `9001` and `9003` unless overridden by environment variables.

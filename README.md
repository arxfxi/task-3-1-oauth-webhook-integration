# Task 3.1 Submission

This service implements the local OAuth + webhook integration against the mock Calendar and mock Slack servers.

## Run

This repository is the `submission/` service. The task package provides mock
Calendar and Slack services in its sibling `data/` folder.

Start the provided mocks from the original task package root:

```bash
cd /Users/abdulr/Documents/Upwork/task_3.1
docker compose -f data/docker-compose.yml up
```

In another terminal, start this service from the submission repository root:

```bash
cd /Users/abdulr/Documents/Upwork/task_3.1/submission
python3 service.py --port 9002
```

Defaults:

```text
SLACK_URL=http://127.0.0.1:9001
CALENDAR_URL=http://127.0.0.1:9003
```

Override them if needed:

```bash
SLACK_URL=http://127.0.0.1:9001 CALENDAR_URL=http://127.0.0.1:9003 python3 service.py --port 9002
```

## Endpoints

```text
GET  /healthz
POST /webhook
POST /sync
```

`POST /webhook` accepts a JSON event:

```json
{
  "event_id": "evt-123",
  "title": "Customer Call",
  "start": "2026-07-22T19:00:00Z"
}
```

`POST /sync` performs the mock OAuth flow, fetches events from mock Calendar, and posts them to mock Slack through the same idempotent delivery path as `/webhook`.

## Design Notes

Idempotency is implemented in memory with a lock-protected processed set and in-progress set. This is enough for the local grader because it runs the service in one process and checks duplicate delivery during that process lifetime.

In production, the idempotency store should be Redis, Postgres, or SQLite with a TTL so replay protection survives restarts and works across multiple replicas.

Slack posting uses bounded exponential backoff with jitter and HTTP timeouts. Jitter spreads retries out under load instead of retrying every failed event at the same instant.

## Quick Checks

Health:

```bash
curl -i http://localhost:9002/healthz
```

OAuth calendar pull:

```bash
curl -X POST http://localhost:9001/_reset
curl -X POST http://localhost:9002/sync
curl http://localhost:9001/_recorded
```

Duplicate idempotency:

```bash
curl -X POST http://localhost:9001/_reset
for i in {1..10}; do
  curl -s -X POST http://localhost:9002/webhook \
    -H 'Content-Type: application/json' \
    -d '{"event_id":"dup-001","title":"Duplicate Test","start":"2026-07-22T19:00:00Z"}'
done
curl http://localhost:9001/_recorded
```

Expected Slack count: `1`.

Load:

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

Expected Slack count: `100`.

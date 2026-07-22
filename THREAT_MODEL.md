# Threat Model

## Replayed Webhooks

Attack: An attacker or buggy upstream system can replay the same event many times, causing duplicate Slack posts.

Mitigation: The service uses `event_id` as an idempotency key and suppresses duplicate or already in-progress events. In production this should use Redis, Postgres, or SQLite with a TTL so replay protection survives restarts and works across replicas.

## Forged Webhooks

Attack: Anyone who can reach `/webhook` could submit fake events and cause unwanted Slack messages.

Mitigation: A production deployment should require a signed webhook header or shared secret, verify the signature over the raw request body, reject stale timestamps, and serve the endpoint only over TLS.

## Oversized Or Malformed Payloads

Attack: A client can send very large bodies, invalid JSON, or unexpected payload shapes to exhaust memory or trigger parsing errors.

Mitigation: The service enforces a maximum request body size, requires valid JSON objects, requires `event_id`, and uses safe defaults for optional fields. Production hardening should add stricter schema validation and request rate limits.

## Retry Storms And Downstream Denial Of Service

Attack: If Slack is slow or unavailable, many events could retry simultaneously and overload the downstream service or this integration.

Mitigation: Slack posts use bounded retries, exponential backoff, jitter, and request timeouts. Production hardening should add queue limits, circuit breakers, and per-tenant rate limits.

## Token Or Secret Leakage

Attack: OAuth tokens, Slack tokens, client secrets, or webhook secrets could leak through logs, source control, screenshots, or error messages.

Mitigation: Configuration should come from environment variables or a secret manager, logs should redact credentials, tokens should be least privilege and rotated regularly, and secrets should never be committed.

## SSRF Through Configurable URLs

Attack: If `SLACK_URL` or `CALENDAR_URL` can be controlled by an attacker, the service could be tricked into posting to internal network resources.

Mitigation: Production deployments should restrict outbound destinations with allowlists, network policy, and configuration ownership controls. The local task defaults to loopback mock services.

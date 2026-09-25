# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Report privately to the maintainer
(leventkurt@gmail.com) with:

- what the issue is and how you reached it;
- the affected version, OS, and Python version;
- reproduction steps or a proof of concept;
- what an attacker gains.

You should get an acknowledgement within a few days. Fixes for confirmed issues
are released as soon as they are ready, and credited in `CHANGELOG.md` unless
you prefer otherwise.

## Threat model

Muninn is a **single-user, self-hosted service**. It is designed to run on your
own machine or your own private host, and it assumes the operator is the only
user. Read this section before exposing it to a network you do not control.

### What Muninn does protect against

- **Server-Side Request Forgery.** `GET /scrape` will not fetch loopback,
  link-local (including cloud metadata such as `169.254.169.254`), private
  RFC1918, multicast or reserved addresses. Hostnames are resolved and *every*
  returned address is checked. Non-`http(s)` schemes are rejected. This is
  enforced twice: in the API and again in the scrape worker, which does not
  trust its caller.
  - Escape hatches, for operators who need them:
    `SCRAPE_ALLOW_PRIVATE_TARGETS=true` or an `SCRAPE_ALLOWED_HOSTS` allowlist.
  - Known limitation: the check is pre-flight, so a hostname whose DNS answer
    changes between validation and connection (DNS rebinding) can still slip
    through. Fully closing this requires pinning the validated IP into the
    transport, which is not implemented.
- **robots.txt.** With `SCRAPE_RESPECT_ROBOTS=true` (the default) each target is
  checked against its origin's `robots.txt` and disallowed paths are refused
  with HTTP 403. An unreachable or missing `robots.txt` fails open.
- **Abuse by accident.** A bounded per-client token bucket on `/scrape` returns
  `429` with `Retry-After` once a client exceeds
  `SCRAPE_RATE_LIMIT_PER_MINUTE` (default 60). Idle client state is swept and
  the table is capped, so the limiter cannot itself be used to exhaust memory.
- **Memory exhaustion from untrusted input.** Response bodies are read with a
  hard byte cap, the scrape cache is a bounded LRU, and per-host politeness
  state is swept.
- **Orphaned browsers.** The scrape worker exits deterministically at idle and
  reaps its node driver and Chromium tree, so a shutdown cannot leave browser
  processes behind.

### What Muninn does NOT protect against

This is intentional: the service is not designed for multi-tenant or
public-internet use, and adding a half-built auth layer would be worse than
documenting the boundary clearly.

- **No authentication or authorization.** Every endpoint - `/search`,
  `/scrape`, `/status` - is open to anyone who can reach the port. There is no
  user model, no API key, and no per-tenant accounting.
  - The service therefore **binds to `127.0.0.1` by default**, and the
    published Docker port is bound to `127.0.0.1` too.
  - `/docs`, `/redoc` and `/openapi.json` are **served by default**. They are
    documentation, not data, but the schema describes every available operation
    — set `DOCS_ENABLED=0` on any deployment you do not control.
  - If you need it reachable from elsewhere, put it behind a reverse proxy that
    does the authentication, or build authentication into your own fork. That is
    your responsibility, not the project's.
- **No rate limiting on `/search`, and the search queue is unbounded.** Each
  request waits at most `REQUEST_TIMEOUT_SECONDS`, so the queue drains eventually,
  but an anonymous caller can grow it faster than the 15–30s throttler empties
  it. `POST` a rate limit in front of `/search` if you expose it.
- **DNS rebinding**, as described above.
- **Sandbox-escape hardening.** Chromium is launched with `--no-sandbox` by
  default (`BROWSER_NO_SANDBOX`), which is what makes it work in minimal
  containers. Combined with a browser that visits attacker-controlled pages,
  keep the container unprivileged and its filesystem read-only, as
  `docker-compose.yml` does. Set `BROWSER_NO_SANDBOX=false` and grant the
  sandbox the capabilities it needs if your environment allows it.

## Hardening checklist for internet-facing deployments

1. Keep the API on `127.0.0.1` behind an authenticating reverse proxy.
2. Leave `SCRAPE_RESPECT_ROBOTS=true`.
3. Leave `SCRAPE_ALLOW_PRIVATE_TARGETS=false` and `SCRAPE_ALLOWED_HOSTS` empty.
4. Set `DOCS_ENABLED=0`.
5. Rate-limit `/search` at the proxy; it has no built-in limit.
6. Keep the container unprivileged with a read-only root filesystem and
   `cap_drop: ALL`.
7. Keep `SCRAPE_RATE_LIMIT_PER_MINUTE` at or below the default.

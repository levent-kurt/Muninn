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
  - **Redirects are followed by hand and every hop is re-validated**, capped at
    `SCRAPE_MAX_REDIRECTS`. Letting the HTTP client follow them would make the
    guard first-hop-only, so a public host answering `302 Location:
    http://169.254.169.254/` could reach the metadata service. A rejected
    redirect target is reported as `400`, not as a transport failure.
  - Escape hatches, for operators who need them:
    `SCRAPE_ALLOW_PRIVATE_TARGETS=true` or an `SCRAPE_ALLOWED_HOSTS` allowlist.
  - Known limitation: the check is pre-flight, so a hostname whose DNS answer
    changes between validation and connection (DNS rebinding) can still slip
    through. Fully closing this requires pinning the validated IP into the
    transport, which is not implemented.
    - **When this stops being acceptable:** on a host anyone but you can reach -
      a public VPS, a shared host, or any deployment behind `--host 0.0.0.0` on
      a network you do not fully control. There, `127.0.0.1` is other tenants'
      services and cloud instance metadata is reachable, so "send me a URL"
      becomes "read internal HTTP". On a private network the guard is doing
      accident-prevention, which it does well; do not treat it as a security
      boundary there.
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
  - The service therefore **binds to `127.0.0.1` by default**. The documented
    production command and `deploy/muninn-api.service` override that with
    `--host 0.0.0.0`, which removes the protection - see the hardening checklist
    before doing so.
  - `/docs`, `/redoc` and `/openapi.json` are **served by default**. They are
    documentation, not data, but the schema describes every available operation
    — set `DOCS_ENABLED=0` on any deployment you do not control.
  - If you need it reachable from elsewhere, put it behind a reverse proxy that
    does the authentication, or build authentication into your own fork. That is
    your responsibility, not the project's.
- **Rate limits are coarse, not a control against a deliberate attacker.**
  Both endpoints have a bounded per-client token bucket
  (`SEARCH_RATE_LIMIT_PER_MINUTE`, default 30; `SCRAPE_RATE_LIMIT_PER_MINUTE`,
  default 60) and the search queue is bounded (`MAX_SEARCH_QUEUE`, default 100),
  so one client cannot monopolise the browser or grow the backlog. Requests that
  hit a limit get `429` or `503` with `Retry-After`. The keys are peer IPs,
  which are trivially spoofed behind a proxy and shared by everyone behind NAT.
  They keep an accidental flood out; if the service faces a hostile network,
  rate-limit at the proxy as well.
- **DNS rebinding**, as described above.
- **Sandbox-escape hardening.** Chromium is launched with `--no-sandbox` by
  default (`BROWSER_NO_SANDBOX`), because its sandbox is unreliable under a
  service manager. Combined with a browser that visits attacker-controlled
  pages, that means a Chromium escape is a compromise of the service account -
  so run Muninn as an unprivileged user, never as root. Set
  `BROWSER_NO_SANDBOX=false` if your host lets Chromium keep its sandbox.

## Hardening checklist for internet-facing deployments

1. Keep the API on `127.0.0.1` behind an authenticating reverse proxy. If you
   must bind `0.0.0.0`, put a firewall in front of port 9999 as well.
2. Leave `SCRAPE_RESPECT_ROBOTS=true`.
3. Leave `SCRAPE_ALLOW_PRIVATE_TARGETS=false` and `SCRAPE_ALLOWED_HOSTS` empty.
4. Set `DOCS_ENABLED=0`.
5. Rate-limit at the proxy as well. The built-in limits are keyed on peer IPs,
   which are spoofable behind a proxy and shared behind NAT.
6. Run it as an unprivileged account (see `deploy/muninn-api.service`), never
   as root, given `--no-sandbox` is on by default.
7. Keep `SCRAPE_RATE_LIMIT_PER_MINUTE` and `SEARCH_RATE_LIMIT_PER_MINUTE` at or
   below their defaults, and leave `MAX_SEARCH_QUEUE` at its default.
8. Do not expose `/metrics` beyond a trusted network: it publishes traffic
   volumes and latency shape.

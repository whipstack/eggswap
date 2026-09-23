# Codex `account/rateLimits/read` — live sample (eggswap)

Bounded child probe, 300s wall-clock budget. Builds on
`docs/research/eggswap/codex-ratelimits-probe.md` (static `strings`-only
analysis by a sibling; no live call). This probe converts that sibling's
UNKNOWN-by-value fields to OBSERVED with one live JSON-RPC round trip.

## 0. Transport used (the safe path)

Did **not** touch `~/.codex/app-server-control/app-server-control.sock` and
did **not** invoke `codex app-server daemon` or `codex app-server proxy` —
those talk to the operator's live shared daemon (pid recorded in
`~/.codex/app-server-daemon/app-server.pid` in the sibling probe), which this
brief was explicitly told not to touch.

Instead: spawned a bare `codex app-server` (no subcommand) as **my own child
process** with fresh stdin/stdout pipes, via Python `subprocess.Popen`. This
starts an independent in-process app-server instance scoped to that process's
own stdio — it is not the daemon and does not dial its control socket. It
reuses `~/.codex` on disk (`CODEX_HOME` unset → default), so it rode the
operator's existing ChatGPT auth without touching any credential file itself.
Killed with `proc.terminate()` (graceful) in a `finally` block once done; no
lock files were created, removed, or forced.

Handshake, newline-delimited JSON-RPC over stdio (one JSON object per line,
no `Content-Length` framing — confirmed empirically, not assumed):

```json
{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "eggswap-probe", "title": "eggswap probe", "version": "0.0.1"}}}
```
then, after reading the `initialize` result:
```json
{"method": "initialized", "params": {}}
{"id": 2, "method": "account/rateLimits/read", "params": {}}
```

`observed_at` was stamped as the wall-clock time (`time.time()`, UTC) at
which the reply line for id=2 was fully read from the child's stdout —
**not** any field inside the response (see §3 for why that distinction
matters).

## 1. Init handshake — OBSERVED

Result for id=1:

```json
{
  "userAgent": "eggswap-probe/0.156.1 (Mac OS 26.5.2; arm64) Apple_Terminal/470.2 (eggswap-probe; 0.0.1)",
  "codexHome": "$HOME/.codex",
  "platformFamily": "unix",
  "platformOs": "macos"
}
```

Between the `initialize` result and the `account/rateLimits/read` result,
two unsolicited notifications arrived on their own, unprompted:

```json
{"method": "remoteControl/status/changed", "params": {"status": "disabled", "serverName": "REDACTED-hostname", "installationId": "REDACTED", "environmentId": null}, "emittedAtMs": 1790169994146}
{"method": "account/updated", "params": {"authMode": "chatgpt", "planType": "pro"}, "emittedAtMs": 1790169994611}
```

Note: the push method that fired here is **`account/updated`**, not
`account/rateLimits/updated`. The sibling probe's `AccountUpdatedNotification`
string-table hit corresponds to this method. `account/rateLimits/updated`
specifically did **not** fire during this ~20s session (see §4).

## 2. `account/rateLimits/read` — redacted response — OBSERVED

Request: `{"id": 2, "method": "account/rateLimits/read", "params": {}}`

Response (`accountId` and the reset-credit `id` are token-shaped/account-
identifying and are REDACTED; every other field is verbatim):

```json
{
  "id": 2,
  "result": {
    "ordinaryUsageAllowed": true,
    "rateLimits": {
      "limitId": "codex",
      "limitName": null,
      "normalModelSlug": null,
      "primary": {
        "usedPercent": 37,
        "windowDurationMins": 10080,
        "resetsAt": 1790580677
      },
      "secondary": null,
      "credits": {
        "hasCredits": false,
        "unlimited": false,
        "balance": "0"
      },
      "individualLimit": null,
      "spendControlReached": false,
      "planType": "pro",
      "rateLimitReachedType": null
    },
    "rateLimitsByLimitId": {
      "codex": { "...": "identical to rateLimits above, keyed by limitId" }
    },
    "rateLimitResetCredits": {
      "availableCount": 1,
      "credits": [
        {
          "id": "REDACTED",
          "resetType": "codexRateLimits",
          "status": "available",
          "grantedAt": 1790109043,
          "expiresAt": 1792701043,
          "title": "Full reset",
          "description": "Thanks for using Codex! You've been granted one free rate limit reset."
        }
      ]
    },
    "accountId": "REDACTED",
    "rateLimitUpsell": null
  }
}
```

Corrections to the sibling's PLAUSIBLE-tier guesses, now OBSERVED:

- The struct the sibling called `RateLimitSnapshot` is real but is wrapped
  one level deeper than guessed: the top-level result key is `rateLimits`
  (singular field on the response, not a bare snapshot), duplicated under
  `rateLimitsByLimitId` keyed by `limitId`.
- `resetsAt` **is** camelCase on the wire (sibling flagged the camelCase
  spelling as unconfirmed) — confirmed here.
- `RateLimitWindow`'s third field is `resetsAt` as a **Unix epoch seconds
  integer** (`1790580677`), not a duration or ISO string.
- `secondary` was `null` in this account/session — this account only has one
  active window (`primary`, a 7‑day/10080‑minute rolling window). The
  sibling's guess that `primary`/`secondary` represent e.g. 5h vs 7d windows
  could not be confirmed either way here since only one was populated.
- `CreditsSnapshot` fields are exactly `hasCredits`/`unlimited`/`balance` as
  the sibling guessed, confirmed; `balance` is a **string** (`"0"`), not a
  number.
- New, not predicted by the static probe at all: `ordinaryUsageAllowed`
  (bool), `rateLimitReachedType` (null here), `rateLimitResetCredits` (a
  separate one-time-reset-credit ledger, unrelated to `CreditsSnapshot`), and
  `rateLimitUpsell` (null here).

## 3. The freshness question — OBSERVED (direct answer)

**The response does not carry its own observation timestamp.** Every
timestamp in the payload (`resetsAt`, `grantedAt`, `expiresAt`) is a
**future or past fixed epoch tied to the rate-limit window or credit grant
itself**, not a "this snapshot was computed at T" field. There is no
`observedAt`, `asOf`, `timestamp`, or similar field anywhere in the
`account/rateLimits/read` result.

**The caller must stamp arrival time itself.** This directly answers the
eggswap `QuotaWindow.observed_at` design question: for this Codex adapter,
`observed_at` has to be assigned by the eggswap client at the moment the
JSON-RPC reply is received (as this probe did), exactly the way
`eggswap/core/types.py`'s `QuotaWindow` already models it (`observed_at` as
caller-supplied, distinct from `resets_at`). A `Source.OBSERVED` reading is
therefore only as fresh as the polling cadence the adapter chooses — there
is no server-side staleness marker to fall back on.

## 4. Unsolicited `account/rateLimits/updated` — not seen this session

Across the full ~20s the child process was alive (`initialize` → both
notifications above → `account/rateLimits/read` round trip → graceful
terminate), the only unsolicited push was `account/updated`
(auth-mode/plan-type, §1), not a rate-limit-specific push. No
`account/rateLimits/updated` notification arrived. This is a **negative
observation bounded by a ~20s window**, not proof the method never fires —
the sibling's `AccountUpdatedNotification` string-table hit is satisfied by
`account/updated` alone, so evidence for a *separate* rate-limits-specific
push notification remains UNKNOWN, now with one clean short-session miss
added to the sibling's UNKNOWN.

## 5. Per-field acceptance table

| bucket | used% | window | reset_at | observed_at | freshness | status |
|---|---|---|---|---|---|---|
| `primary` (limitId `codex`) | `37` | `10080` min = 7d | `1790580677` = 2026-09-28T07:31:17Z | stamped by caller at reply arrival = 2026-09-23T13:26:52.19Z | no server field; poll-only, confirmed no built-in staleness marker | OBSERVED |
| `secondary` | — | — | — | — | — | OBSERVED-null (field exists, unpopulated for this account) |
| `credits` | balance `"0"`, `hasCredits=false` | n/a | n/a | same reply | same as above | OBSERVED |
| `rateLimitResetCredits[0]` | n/a | n/a | `expiresAt=1792701043` (2026-10-22T20:30:43Z) | same reply | same as above | OBSERVED |
| `account/rateLimits/updated` push | n/a | n/a | n/a | n/a | did it fire during session? | UNKNOWN (not seen in ~20s window, see §4) |
| reply's own freshness field | n/a | n/a | n/a | n/a | does the response self-report capture time? | OBSERVED — **no**, absent from schema |

## 6. What the Codex adapter may now legitimately report, and what it still may not

The eggswap Codex adapter may now report a `QuotaWindow` built from
`primary` with `Source.OBSERVED` for `used_percent`, `window_seconds`
(`windowDurationMins * 60`), and `resets_at` (`resetsAt` verbatim, already
epoch seconds) — all three are confirmed real wire fields with confirmed
types from this live sample, not inferred names. `observed_at` must be
stamped by the adapter itself at reply-arrival time, exactly as
`QuotaWindow` already requires; there is no server-supplied alternative to
fall back on, so a stale poll cannot be detected from the payload alone —
only from how long the adapter's own clock says has elapsed since it last
polled. The adapter should also plan for `secondary` being legitimately
`null` (single-window accounts exist; this is not an error case) and must
not assume `secondary`, when present, means a shorter/5h window — that
mapping remains unconfirmed.

The adapter may **not** yet claim a confirmed schema for `secondary`
populated (no account in reach of this probe had a non-null one), may not
claim `account/rateLimits/updated` exists or does not exist as a distinct
push method (still UNKNOWN, only `account/updated` was confirmed live), and
may not assume `limitId` values beyond the single observed `"codex"` (e.g.
whether a Plus/Team/Enterprise plan surfaces additional `limitId` keys in
`rateLimitsByLimitId`). Those remain gaps for a future bounded probe with a
different account or a longer observation window.

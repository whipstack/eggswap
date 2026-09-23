# Codex `account/rateLimits/updated` push — extended probe (eggswap)

Bounded child probe, 300s wall-clock budget. Builds directly on
`docs/research/eggswap/codex-ratelimits-live.md`, which left this open:

> "account/rateLimits/updated as a push notification: still UNKNOWN. It did
> not fire in a ~20s window. Only account/updated was seen live. One clean
> short-session miss, not a refutation."

This probe reused that sibling's handshake unchanged and extended the
observation window to 150s, with periodic `account/rateLimits/read` calls
during the window (to test whether a *read* triggers a distinct *push*).

## Transport (same safe path as the sibling)

Did **not** touch `~/.codex/app-server-control/app-server-control.sock` and
did **not** invoke the daemon/proxy subcommands. Spawned the real binary
directly — `$HOME/.codex/packages/standalone/current/bin/codex
app-server` — as this probe's own child via Python `subprocess.Popen` with a
list argv (bypasses the shell's `codex` wrapper function entirely, so no
p2p-attach socket was ever dialed). Talked newline-delimited JSON-RPC over
its stdin/stdout. Killed with `proc.terminate()` in a `finally` block; no
lock files touched, no login/logout, no daemon restart.

## 1. Static check — OBSERVED (new evidence, not in the sibling's file)

`strings` on the same binary, filtered to `/`-delimited wire method names,
finds a contiguous block that is clearly a single serde adjacently-tagged
enum's variant list (`ServerNotification`, confirmed by the literal tag text
sitting immediately before it in the string table):

```
...mcpServer/event/stream/notification
account/updated
account/rateLimits/updated
remoteControl/status/changed
externalAgentConfig/import/progress
...
```

`account/rateLimits/updated` sits **directly adjacent to** `account/updated`
inside this `ServerNotification` variant list — the same enum that contains
every notification method actually observed live (`account/updated`,
`remoteControl/status/changed`, etc., both here and in the sibling's file).
It is *not* in the request/response method list (that separate block —
`account/login/start`, `account/rateLimits/read`, `account/usage/read`,
`config/read`, ...) and it is not attached to an error-message string. This
is strong static evidence the method is a **defined, real notification
type** the server knows how to emit, wired into the same enum as a
notification we've already watched fire — not a name pulled from an error
string.

This confirms the *type exists*. It does not by itself confirm *when the
server chooses to emit it* — that requires a live trigger, attempted below.

## 2. Live check — extended window, OBSERVED-NO (bounded)

- Window: **150 seconds** of continuous stdio read after `initialized`,
  versus the sibling's ~20s.
- During the window, this probe additionally sent **5** explicit
  `account/rateLimits/read` requests, spaced 30s apart, specifically to test
  whether a *read* is what triggers the *push* (e.g. server recomputes and
  broadcasts on read, the way some servers pair a read RPC with a fan-out
  notify). All 5 got ordinary synchronous replies (ids 2–6, all answered).
- **Complete list of unsolicited notification methods seen, whole session:**
  - `remoteControl/status/changed` (t≈0.89s, once, at startup)
  - `account/updated` (t≈1.59s, once, at startup)
  - nothing else arrived — including during or immediately after any of the
    5 `account/rateLimits/read` round trips.
- `account/rateLimits/updated` did **not** fire at any point in the 150s
  window, including immediately following an explicit read of the same
  data.

This is the same idle-account, single-session setup as the sibling
(personal `pro` plan account, no concurrent Codex usage driving the quota
during the window), just 7.5x longer and with active reads interleaved. It
raises the confidence of a negative result but does not eliminate the
gap the sibling already named: an idle account over minutes says little
about whether usage-driven quota changes (an actual API call consuming
tokens against the rolling window) are what triggers the push, if anything
does. Neither this probe nor the sibling's exercised the account while
holding the session open — that specific trigger (usage-under-open-session)
remains untested.

## 3. Verdict

**STILL-UNKNOWN**, but narrowed:

- Window: 150s (this probe) + ~20s (sibling) = ~170s combined idle-account
  observation, zero occurrences.
- Methods that DID arrive, both sessions combined: `remoteControl/status/changed`,
  `account/updated`. Nothing else, ever.
- What changed: the method name is now **statically confirmed to be a real,
  defined `ServerNotification` variant** (§1) — this rules out "it doesn't
  exist as a wire concept" as an explanation for the silence. What remains
  unconfirmed is the trigger condition: does the server push it (a) on any
  read of rate-limit data, (b) only when usage actually changes the
  underlying numbers, (c) only on a longer timer, or (d) never in current
  builds despite being defined (e.g. reserved for a future release). This
  probe rules out (a) — an explicit read did not trigger it — but cannot
  distinguish (b), (c), or (d) without driving real token usage against the
  account during an open session, which is outside this probe's scope
  (would consume the operator's live quota) and outside its 5-minute budget.

## 4. What the eggswap Codex adapter may change as a result

**Nothing — today's poll-and-stamp design is confirmed correct, now with
stronger evidence.** `QuotaWindow.observed_at` must stay caller-stamped at
read time; `Source` must stay `OBSERVED` only for a just-completed read and
degrade to `Unknown(stale_since=...)` past the adapter's freshness window,
exactly as `eggswap/core/types.py` already models. Two probes totaling
~170s of idle-account observation, one of them holding a session open
across five explicit rate-limit reads, produced zero unsolicited
rate-limit-specific pushes — a live subscribe-and-cache path is not
demonstrated to exist and the adapter should not be built to assume one.
The one actionable follow-up, if a future bounded probe has budget and an
account with headroom to spend, is to issue a real Codex request that
consumes quota while the JSON-RPC session stays open and watch specifically
for `account/rateLimits/updated` immediately after — that isolates trigger
(b) above and is the only remaining untested path to an OBSERVED-YES.

# Codex capacity-signal probe (eggswap)

Bounded child probe, 300s wall-clock budget. Method: static analysis of the
installed Codex binary (`strings` + grep) and on-disk logs. **No live
JSON-RPC call to the app-server was made** — see §5 for why, and what would
close that gap. No secret values were printed; only field/struct names.

## 0. What's installed

- `codex` in this shell is a wrapper function (defined in the zsh snapshot
  sourced by this session) that dispatches subcommands like `login`,
  `mcp-server`, `app-server` straight to `command codex`, and anything else
  through `$WHIPSTACK_P2P_ROOT/bin/codex-p2p-attach`.
  Command: `type codex`.
- The real binary resolved by `command codex` is
  `$HOME/.local/bin/codex` (`which -a codex`), which is a shim into
  the standalone release:
  `$HOME/.codex/packages/standalone/releases/0.156.1-aarch64-apple-darwin/bin/codex`
  (238 MB Rust binary; sibling `codex-code-mode-host`, 62 MB).
  Version: `codex --version` (via the wrapper) → `codex-cli 0.156.1`.
- An app-server daemon is running: `~/.codex/app-server-daemon/app-server.pid`
  → `{"pid":62610,"processStartTime":"Sat Sep 5 01:55:55 2026"}`, confirmed
  live in `ps aux` at probe time.
- A control socket exists at
  `~/.codex/app-server-control/app-server-control.sock`. One earlier command
  in this session (`codex --version` through the p2p wrapper) logged:
  `"state":"DETACHED_FALLBACK","detail":"socket-file-exists-but-connect-was-refused:starting-plain-codex"`.
  That's evidence the control socket did not accept a connection at that
  moment for that client — not proof the app-server RPC itself is down (the
  daemon pid above is separate from that control socket). UNKNOWN whether a
  direct JSON-RPC dial to the app-server itself would succeed; not attempted
  (see §5).

## 1. Does this build support `account/rateLimits/*`? — OBSERVED

Command: `strings <binary> | grep -i rateLimits` (and `-i rate_limit`),
where `<binary>` is
`~/.codex/packages/standalone/releases/0.156.1-aarch64-apple-darwin/bin/codex`.

Literal hits (exact strings from the binary, method names as sent over the
app-server JSON-RPC protocol):

- `account/rateLimits/read failed for thread usage in TUI`
- `account/rateLimits/read failed during TUI refresh: `
- `account/usage/read failed for thread usage in TUI`
- `account/workspaceMessages/read failed in TUI`
- `AccountUpdatedNotification` (adjacent to the same string cluster as
  `rateLimits`, `authMode`, `DesktopOnboardingEntrypoint`) — consistent with
  a push notification carrying rate-limit updates, but I did not isolate its
  field list from the other account-update payloads it's clustered with, so
  I report the method's *existence* as OBSERVED and its *exact schema* as
  UNKNOWN (see the "updated" row in §3).

Conclusion: **this installed build (0.156.1) does support reading rate
limits** — the TUI itself calls `account/rateLimits/read` (the error strings
are the TUI's own failure-path messages, which only exist because the TUI
calls that method). This is the strongest evidence available without a live
call: the client code that ships in this binary depends on the method.

## 2. Response shape — OBSERVED (struct/field names only, via `strings`)

Rust binaries retain serde struct/field names and debug-derive metadata in
the string table even when stripped of other symbols; `strings` surfaces
them without needing a live call. All from the same binary as §1.

- `RateLimitSnapshot` — debug metadata says "struct RateLimitSnapshot with
  10 elements". Field names recovered from adjacent string runs (snake_case
  Rust side and camelCase wire side both present, i.e. this type has a serde
  rename layer): `limit_name`/`limitName`, `normal_model_slug`/
  `normalModelSlug`, `primary`, `secondary`, `credits`,
  `individual_limit`/`individualLimit`, `spend_control_reached`/
  `spendControlReached`, `plan_type`. That's 8 of the 10 named fields I could
  positively attribute from string adjacency; the remaining 2 are UNKNOWN
  (the flat string table doesn't guarantee I've attributed every string to
  the right struct — I only report names I saw directly beside the
  `RateLimitSnapshot` debug-derive marker or in a tight, repeated cluster).
- `RateLimitWindow` — "struct RateLimitWindow with 3 elements". Recovered:
  `percent`/`usedPercent`, `window_minutes`/`windowDurationMins`, and
  `resets_at` (seen snake_case, directly adjacent: `...percentwindow_minutesresets_at...`).
  I did not separately confirm the camelCase spelling of the third field
  (expected `resetsAt` by the project's own snake→camel convention seen on
  the other two, but not directly observed adjacent in a single string run)
  — flag that spelling as PLAUSIBLE, not OBSERVED.
- `CreditsSnapshot` — "struct CreditsSnapshot with 3 elements":
  `hasCredits`, `unlimited`, `balance`.
- Elsewhere, a distinct/older-looking pair `limitId` + `resetsAt` appears
  together near `usageLimitExceeded`, and a separate `LimitSnapshot`-shaped
  cluster has `used`, `remainingPercent` — these may be a different code
  path (e.g. spend-control limits vs. plan rate limits) rather than
  `RateLimitWindow` itself. UNKNOWN which exact JSON-RPC response envelope
  wraps `RateLimitSnapshot`/`RateLimitWindow` — that requires either reading
  the (not present on this disk) source or making a live call.

No live response was captured, so I cannot report real bucket ids, a real
`usedPercent` value, a real window duration, or a real `resets_at` value —
only that these fields exist in the shipped protocol.

## 3. Per-field status (acceptance table)

| eggswap field | status | evidence / probe |
|---|---|---|
| bucket (which window, e.g. 5h/weekly/credits) | OBSERVED (name only) | `RateLimitSnapshot.primary` / `.secondary` / `.credits` fields exist, per §2. Real bucket identity (which is 5h vs 7d) not observed — would need a live response or source read. |
| used% | OBSERVED (name only) | `RateLimitWindow.percent`/`usedPercent`, per §2. |
| window (duration) | OBSERVED (name only) | `RateLimitWindow.window_minutes`/`windowDurationMins`, per §2. |
| reset_at | PLAUSIBLE (name only, weaker) | `resets_at` seen snake_case directly beside `percent`/`window_minutes` in the same struct's string run, per §2. camelCase spelling and exact struct attribution not independently confirmed. |
| observed_at (this probe's capture time) | UNKNOWN — no live sample was taken | No live call to `account/rateLimits/read` was made (see §5), so there is no "observed_at" for an actual snapshot — only for this static-analysis pass, and a probe timestamp for a binary scan isn't the same signal eggswap needs. Probe: dial the app-server over `~/.codex/app-server-control/app-server-control.sock` (or spawn `codex app-server` directly) and issue `account/rateLimits/read`; stamp the reply's arrival time. |
| freshness (age / push vs. poll) | UNKNOWN | `AccountUpdatedNotification` exists (§1) suggesting a push path, but its trigger cadence and payload weren't isolated from the surrounding account-update strings. Probe: subscribe to app-server notifications for one session and grep the stream for `AccountUpdatedNotification` occurrences and their timestamps. |

## 4. On-disk rate-limit / 429 evidence

- `~/.codex/history.jsonl`: `grep -c -i "rate_limit\|rateLimit\|429"` → 2
  matches. Manually inspected (structure only, no content dumped): **both
  are false positives** — each matched line's Unix epoch `"ts"` value
  happens to contain the substring `429` (e.g. `...,"ts":1788429205,...`),
  not an actual rate-limit or HTTP 429 event. Clean negative: no genuine
  rate-limit/429 evidence in `history.jsonl` at probe time.
- `~/.codex/log/`: only file present is `codex-login.log` (336 bytes).
  `grep -ci "rate\|429" codex-login.log` → 0. Clean negative.
- Scope searched: exactly `~/.codex/history.jsonl` and every file directly
  under `~/.codex/log/` (one file). Did not search `~/.codex/logs_2.sqlite`
  (232 MB, binary/WAL-backed sqlite — out of scope for a grep-based probe in
  this budget; querying it for rate-limit rows is a follow-up probe, not
  done here).

## 5. Why no live call, and what would close the gap

This probe was static (binary strings + on-disk file grep) because standing
up a JSON-RPC client against the app-server's stdio or control-socket
protocol, framing a well-formed `account/rateLimits/read` request, and
parsing the response was not achievable inside the remaining budget on top
of the discovery work above. The one live-transport signal collected was
incidental: an earlier `codex --version` call (routed through this
project's `codex-p2p-attach` wrapper) logged a `DETACHED_FALLBACK` state
because a connect to `app-server-control.sock` was refused at that instant —
this says something about that wrapper's control-plane dial, not about
whether `account/rateLimits/read` itself would succeed against the running
app-server daemon (pid 62610, confirmed alive).

Next probe (not run here): use `codex app-server` directly (it speaks
JSON-RPC over stdio per the method names in §1), send a minimal
`{"method":"account/rateLimits/read","id":1,"params":{}}` after the
protocol's initialize handshake, and capture the raw reply shape — that
converts every PLAUSIBLE/UNKNOWN row in §3 to OBSERVED or a named failure.

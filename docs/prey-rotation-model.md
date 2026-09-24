# Prey rotation model: realiti4/claude-swap (installed copy)

Source read: `$HOME/.local/share/uv/tools/claude-swap/lib/python3.14/site-packages/claude_swap/`
(installed package on disk; no clone, no network). Method for every claim below: `grep`/`Read`
of the named file at the cited line, same session. No values, only names/shapes — no
credential material was printed.

## 1. Account record

Identity fields (`models.py:79-120`, `AccountInfo` dataclass):
`email: str`, `uuid: str`, `organization_uuid: str`, `organization_name: str`, `added: str`,
`number: int` (slot index, not part of the API payload — assigned locally). `is_organization`
(`models.py:90-92`) is just `bool(organization_uuid)`. `from_dict`/`to_dict` (`models.py:100-120`)
map to/from Anthropic's OAuth-profile JSON keys (`organizationUuid`, `organizationName`).

Credential storage/routing lives in `CredentialStore` (`credentials.py:296-`), not in models.py.
It is a **leaf collaborator** that only imports `macos_keychain` and `paths`, never `switcher`
(`credentials.py:8-16`) — the switcher passes it a read-only "host view" so storage and
orchestration can't re-couple. State it owns: `_keychain_usable_cache` (sticky, process-local,
tri-state None/True/False) and `_last_active_credentials_backend` (`credentials.py:300-337`).

Routing logic: `_kc_call` (`credentials.py:339-384`) runs a keychain op and, on any
`macos_keychain.KEYCHAIN_ERRORS`, sets `_keychain_usable_cache = False` plus a monotonic cooldown
deadline (`_keychain_disabled_until`, `credentials.py:361-368`) so one invocation can't split-brain
between file and keychain backends mid-run. `_use_keychain()` (`credentials.py:387-408`) re-probes
only after the cooldown elapses. On macOS the keychain **service name** is derived from the
(NFC-normalized) `CLAUDE_CONFIG_DIR` value that Claude Code itself hashes
(`credentials.py:69-104`, cross-referenced in `session.py:8-9`) — cswap doesn't invent a naming
scheme, it re-derives Claude's own per-profile service name so each session profile lands in its
own keychain entry. Plaintext `.credentials.json` is written deliberately as a seed even on macOS
(`session.py:11-16`) because it's Claude's *only* mechanism on Linux and Claude migrates it into
its own hashed keychain entry on first write — writing that keychain entry ourselves would couple
cswap to Claude's internal storage format. The module docstring (`credentials.py:1-16`) also names
an `.enc`-wins backup-reconciliation rule from issue #66; I did not trace that code path within
budget — flagged as **UNKNOWN (not traced)**, not silently skipped.

## 2. Capacity signal

Cache file: `UsageStore.__init__` (`usage_store.py:829-832`) — one file,
`cache_dir / "usage.json"`, locked by a sibling `.usage.lock` (FileLock, `fcntl.flock`,
`locking.py:19-51`). Instantiated at `switcher.py:328` as `UsageStore(self.backup_dir / "cache")`.
On-disk shape: `{"schemaVersion": ..., "accounts": {<slot>: {...row}}}` (`usage_store.py:844-852`);
a row is identity-guarded by `(email, organizationUuid)` (`usage_store.py:855-863`) so a stale
slot never gets silently attributed to a different account after a reassignment.

Freshness stamp is `fetchedAt` (epoch seconds, written only on success —
`usage_store.py:1071-1074`); `age_s = now - fetchedAt` is derived at read time
(`usage_store.py:892`), never stored. The raw percentages/reset timestamps live under
`lastGood` (`usage_store.py:891`, `1073`), fetched from `https://api.anthropic.com/api/oauth/usage`
(`oauth.py:366`, `fetch_usage` at `oauth.py:584`). Window keys returned/parsed: `five_hour`,
`seven_day` (`oauth.py:437-451`), plus a `scoped` list carrying **per-model windows including
Fable** (`oauth.py:479-497`, comment names "Fable" explicitly at `oauth.py:479`).
`relevant_windows()` (`oauth.py:505-539`) flattens all of these into `(label, pct, resets_at)`
tuples, which is what every downstream consumer (headroom, ranking) reads — no caller touches
`five_hour`/`seven_day` raw dict keys directly except the fetch/format layer.

**FAIL vs zero**, in `UsageStore.record()` (`usage_store.py:1041-1106`): success and failure are
"mutually exclusive writers" (docstring, `usage_store.py:1051-1053`). On success:
`row["lastGood"] = rec.usage; row["fetchedAt"] = now; row["consecutiveFailures"] = 0`
(`usage_store.py:1073-1083`). On failure: `row["consecutiveFailures"] += 1`,
`row["lastError"] = rec.error`, a backoff deadline is set — but **`lastGood`/`fetchedAt` are never
touched** (`usage_store.py:1084-1106`, and stated as invariant at `:1052-1053`). So a read failure
leaves the old percentage in place, marked increasingly stale via `age_s`; it does not zero the
value or invent a fresh one. Trust decay for a stale `lastGood` is `_rate_limited_trust_ok()`
(`usage_store.py:484-518`): trusted until `min(earliest relevant-window reset, fetchedAt +
RATE_LIMIT_TRUST_MAX_AGE_S)`, an explicit data-driven bound, not a fixed clock guess
(`usage_store.py:493-510`).

## 3. Rotation decision

Config defaults: `threshold: float = 90.0` (`settings.py:46`),
`strategy: str = "best"  # "best" (most headroom) or "consume-first" (soonest weekly reset)`
(`settings.py:50`). Threshold is user-tunable in `[50.0, 99.9]` (`settings.py:106`).

Headroom arithmetic — the actual number the threshold is compared against —
is `account_headroom()` (`oauth.py:543-561`):
```
pcts = [pct for _, pct, _ in relevant_windows(usage, models)]
return 100.0 - max(pcts)          # oauth.py:558-561
```
i.e. headroom is `100 − max(binding-window utilization)` across 5h, 7d, and any configured scoped
(Fable) windows — the *worst* window binds, not an average.

Threshold gate: `_every_account_above_threshold()` compares
`(100.0 - active_headroom) < threshold` (`autoswitch.py:609`) — i.e. switch is even considered once
the active account's own utilization (`100 - headroom`) reaches the threshold (90 by default).

Selection arithmetic for `strategy == "best"`: in the candidate-ranking block, when neither
`by_recovery` nor `consume_first` applies, `key = (-h,)` (`autoswitch.py:1946`, `h` = headroom of
that candidate), and the candidate list is then `qualifying.sort(key=lambda t: t[0])` ascending
(`autoswitch.py:1948-1949`). Ascending-by-`-h` ⇒ **descending by headroom** ⇒ "best" picks the
candidate with the single most remaining headroom, full stop — sequence order breaks ties only
when headroom is exactly equal. `consume_first` instead sorts by soonest weekly `resets_at` first,
headroom as the tiebreaker (`autoswitch.py:1941-1943`).

## 4. Concurrency

`cswap run` isolation: `CLAUDE_CONFIG_DIR` is pointed at a persistent per-account profile
`<backup_dir>/sessions/<num>-<email-slug>/` (`session.py:3-9`, set at `session.py:571`), which
"fully isolates Claude Code's config and credential lookup" — on macOS this also changes the
per-profile keychain service name because Claude hashes that env-var value (`session.py:7-9`).

What prevents **two processes on one account**: nothing at the `run()` call itself
(`session.py:504-572`) blocks a second concurrent launch against the same `session_dir` — it
`execvpe`s straight into `claude` (`session.py:590-604`) with no lock held across the exec
("the lock is already released — an exec'd claude must never inherit a held flock",
`session.py:593-594`). The one guard in `run()` is a **same-account fast path**
(`session.py:538-551`): if the requested account is already the current *default* login, cswap
skips creating a second profile/credential copy entirely, specifically because "two copies of one
account can drift if the server rotates the refresh token" (`session.py:541-542`) — that's a
drift-avoidance shortcut, not a mutex. Separately, `scan_live_sessions()`
(`session.py:446-457`, backed by `process_detection.py`, PID + start-time verified via `ps` at
`process_detection.py:109-132` to rule out PID reuse) is used to **gate destructive management
operations** (e.g. removing/switching a profile with a live Claude session attached) — it is a
pre-flight check consulted by callers, not a lock that stops two `claude` processes from running
under the same `CLAUDE_CONFIG_DIR` simultaneously. Real locks that do exist: `FileLock` on
`self.switcher.lock_file` around switcher state mutation (`session.py:713`), and
`proper_lockfile` (Claude Code's own npm-compatible directory lock format,
`claude_locks.py:86-186`) guarding `~/.claude.json` writes (`session.py:1178-1182`) — both protect
metadata/config writes, not "am I the only session on this account" at exec time.
**Conclusion: two concurrent `cswap run` processes targeting the same account number are not
prevented** — they'd share one profile dir and one keychain entry with no arbitration beyond
whatever Claude Code itself does internally.

## 5. Judgment

**Gets right:** stale-on-error as a first-class state (§2) — a failed usage fetch never zeros or
guesses a number, it keeps the last measurement and lets a data-derived trust window (soonest
window reset, or an explicit age ceiling) decide when it stops being usable. That single design
choice is what keeps rotation decisions honest under flaky network conditions instead of either
false-tripping on a transient error or blindly trusting arbitrarily old data.

**Won't generalize:** the account/credential identity model is Anthropic-shaped end to end —
`organization_uuid`/`organization_name` fields (§1), a keychain service name derived from
*Claude Code's own* hashing of `CLAUDE_CONFIG_DIR` (§1, §4), a single hardcoded
`api.anthropic.com/api/oauth/usage` endpoint (§2), and a headroom model built on Anthropic's
specific window vocabulary (`five_hour`, `seven_day`, scoped-per-model like Fable, §2-3). None of
that has a slot for a second provider's org model, token-refresh mechanics, or rate-limit window
shape (e.g. OpenAI's differently-scoped headers) without an actual provider-abstraction layer —
eggswap needs to introduce one, not just add a second copy of this code.

## UNKNOWNs (budget-limited, not investigated)

- The `.enc`-wins backup reconciliation from issue #66 mentioned at `credentials.py:1-16` —
  code path not traced.
- Exact poll cadence / scheduler cost that decides *when* a background refresh runs
  (`pace.py`, `_collect_scheduled_usage`, `autoswitch.py:1953+`) — named but not fully read.
- Whether Claude Code's own client-side behavior (outside cswap) provides any same-account
  mutual exclusion that would compensate for the gap found in §4 — out of scope of this
  installed package.

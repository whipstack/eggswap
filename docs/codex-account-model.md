# What IS a Codex account, to a program that wants to rotate several of them?

Measured on this machine, 2026-09-23. Codex CLI version: `codex-cli 0.156.1`
(binary resolved via `readlink`/`realpath` of `~/.local/bin/codex` to
`~/.codex/packages/standalone/releases/0.156.1-aarch64-apple-darwin/bin/codex`).
All commands below were run against this exact binary/config on this machine only —
not verified against other codex versions or other machines.

## 1. How is a codex account identified?

**Method:** `python3 -c "import json;d=json.load(open('$HOME/.codex/auth.json'));print(sorted(d.keys()))"`

Top-level keys of `~/.codex/auth.json`: `['OPENAI_API_KEY', 'auth_mode', 'last_refresh', 'tokens']`

Recursed one level into `tokens` (keys only, values redacted):
`['access_token', 'account_id', 'id_token', 'refresh_token']`

- `auth_mode` = `"chatgpt"` on this machine (a string, not redacted — it's a mode name, not a secret).
- `OPENAI_API_KEY` is `null`/absent (not populated — this machine authenticates via ChatGPT OAuth, not an API key).
- `last_refresh` is present (timestamp of last token refresh).
- The account identity itself is `tokens.account_id`, corroborated by binary strings analysis below.

**Method:** `strings` on the release binary, filtered for account-identity fields:
`strings <binary> | grep -oE '.{0,30}account_id.{0,30}'`

Found the JWT/session claim set the binary parses: `chatgpt_user_id`, `chatgpt_account_id`,
`chatgpt_plan_type`, `chatgpt_account...` (truncated in strings output) alongside `email`.
This confirms the account is a **ChatGPT account** (identified by `chatgpt_account_id` /
`account_id`), not a bare API key — the API-key path (`OPENAI_API_KEY`) is a separate,
mutually-exclusive `auth_mode`.

**Conclusion:** a Codex "account" = one `(auth_mode, account_id, token set)` tuple. On this
machine `auth_mode=chatgpt` and the account is identified by `tokens.account_id`, refreshed
via `tokens.refresh_token`. Only ONE such tuple is stored in `auth.json` — it is a flat object,
not an array/map of accounts (confirmed by the key list above: no nesting like
`accounts.<id>.tokens`).

## 2. Where does the credential live?

`~/.codex/auth.json` (mode 0600 — `-rw-------`, confirmed via directory listing). This is the
**entire** credential store for account identity: `access_token`, `refresh_token`, `id_token`,
`account_id`. There is no secondary per-account credential file found in `~/.codex/` (directory
listing of all 44 entries showed no `auth-*.json`, `accounts/`, or similar).

eggswap uses this evidence conservatively: with multiple configured Codex
profiles, a profile whose effective store is not confirmed as file-backed is
reported `UNKNOWN` and cannot be selected. A single non-file-backed profile
may still use its measured capacity reader, but that does not establish that
another `CODEX_HOME` would reach a separate keyring entry. Keyring namespacing
remains unverified.

## 3. Is `[profiles.*]` in `config.toml` an account credential, or only a preset?

**Method:** read `~/.codex/config.toml` directly (values that looked secret-shaped were
redacted; none were found — this file has no credential fields).

Actual content of `~/.codex/config.toml` on this machine has **no `[profiles.*]` section at
all** — only `[projects."<path>"]` (trust level per project dir), `[features.multi_agent_v2]`,
`[tui]`, `[tui.model_availability_nux]`, and `[marketplaces.*]`/`[plugins.*]`. Top-level `model`
and `model_reasoning_effort` are also present.

This is itself evidence for the crux question: **the config file that exists on this machine
carries zero account/credential material** — it is entirely model/UI/project presets. Nothing in
`config.toml` overlaps with the four `auth.json` token fields. So on this machine, config.toml
cannot be an account credential store even if `[profiles.*]` were populated, because credential
fields (access/refresh/id token, account_id) are structurally confined to `auth.json`.

**UNKNOWN:** whether `[profiles.*]` (not present here) can itself carry a `chatgpt_account_id` or
alternate `auth.json` path override on other installs. Not observed on this machine — no
`[profiles.*]` block exists to inspect. Probe: on a machine/config with `[profiles.*]` populated,
diff its keys against the `auth.json` field set above; if disjoint, profiles are config-only
presets (model/effort/sandbox), consistent with what this machine's docs/binary strings suggest
(`model`, `model_reasoning_effort` are the only non-project top-level keys seen).

## 4. How does a running codex process bind to one account?

**Method:** `strings` on the release binary for `CODEX_HOME` and related env vars; inspection of
`~/.codex/app-server-control/` and `~/.codex/app-server-daemon/`; `env | grep CODEX_HOME`.

- The binary contains the literal string `CODEX_HOME` (confirmed via `strings`), i.e. it reads
  an env var to select the home directory that in turn contains `auth.json`. This machine has
  `CODEX_HOME` **unset** in the current shell (`env | grep -i CODEX_HOME` returned nothing) —
  so the process defaults to `~/.codex`.
- Binder mechanism = **directory, not a separate account-selector env var**: whichever
  `CODEX_HOME` points at supplies exactly one `auth.json`, hence exactly one bound account per
  process, for the lifetime of that process (tokens are refreshed in place, not swapped).
- App-server / daemon layout observed under `~/.codex/`:
  - `app-server-control/app-server-control.sock` — control socket (plus a `.stale-<pid>`
    leftover from a prior run, and `app-server-startup.lock`).
  - `app-server-daemon/{daemon.lock, app-server.pid.lock, app-server.pid,
    app-server-updater.pid.lock, app-server-updater.pid, *.stderr.log, settings.json}` — a
    **single** daemon instance guarded by PID/lock files, one socket. This is a single-daemon,
    single-`CODEX_HOME` design: the lock files and one control socket imply the daemon is scoped
    to one `CODEX_HOME` at a time, not one per account.
  - `thread-writer-locks/*.lock` — per-thread (conversation) write locks, unrelated to account
    identity.
- This repo already has a partial answer in flight: `bin/codex-p2p-attach` (the shell function
  `codex()` on this machine wraps raw `codex` invocations through it) exists specifically to
  attach to a shared app-server socket (`app-server-control.sock`) rather than starting a new
  server per invocation — but `grep -n CODEX_HOME bin/codex-p2p-attach` found **zero
  references**: this wrapper does not currently vary `CODEX_HOME`, so it does not yet implement
  per-invocation account selection.

**UNKNOWN:** the exact set of all env vars the binary consults for auth (beyond `CODEX_HOME`).
Binary strings also contained `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `CODEX_CONNECTORS_TOKEN` in
the vicinity of the OPENAI_* / AWS_*/AZURE_*/GOOGLE_* credential-env-var block, but I did not
verify which of these the CLI (vs. some embedded SDK/tool the binary bundles, e.g. exec-server)
actually reads at startup, nor their precedence vs. `auth.json`. Probe: `codex debug` /
`codex --help` subcommand output, or set each var to a canary value and run `codex login status`
(or equivalent) to see which one changes behavior — not run here due to the time budget and to
avoid touching live auth state outside the one owned file.

## 5. Can two accounts be held simultaneously? What would have to move?

**Not supported natively, and not currently implemented in this repo's wrapper.** Evidence:

- `auth.json` is a single flat object (section 1) — no array/map of accounts, so one
  `CODEX_HOME` cannot hold two accounts at once.
- The daemon/socket/lock layout (section 4) is singular: one `app-server-control.sock`, one
  `app-server.pid.lock`. Two simultaneously-bound accounts under the same `CODEX_HOME` would
  require either two daemons on two sockets, or the daemon to demultiplex requests by account —
  neither is evidenced by the files present.
- `codex-p2p-attach` in this repo already anticipates *some* multi-instance need (it checks for
  a live socket and falls back to `DETACHED_FALLBACK` / "starting plain codex" when the socket
  exists but connection is refused — observed live during this probe via
  `codex --version`'s stderr line:
  `{"fsm":"codex-p2p-attach","state":"DETACHED_FALLBACK",...}`), but this fallback is about
  daemon *liveness*, not account *identity* — it does not vary `CODEX_HOME` or `auth.json`.

**What would have to move to rotate accounts (inferred from the structure above, not yet
implemented or tested here):**
1. Point each account at its own `CODEX_HOME` directory (env var, confirmed to exist) containing
   its own `auth.json` — this mirrors how `cswap` isolates Claude profiles by directory per
   [[eggswap parent goal]], rather than swapping file contents in place.
2. Each `CODEX_HOME` needs its own `app-server-control/` + `app-server-daemon/` (the daemon/lock
   files are directory-scoped, so a distinct `CODEX_HOME` naturally gets a distinct socket/lock
   set — not verified by launching a second daemon in this probe, since that would mutate shared
   process state outside the one owned deliverable file).
3. `bin/codex-p2p-attach` would need to set/forward `CODEX_HOME` per invocation (today it does
   not — confirmed by the zero-match grep above); this is a gap, not yet a capability.

**UNKNOWN:** whether launching two `codex app-server` processes with two different `CODEX_HOME`
values actually works cleanly side-by-side (port/socket collisions, shared cache files like
`models_cache.json` if any live outside `CODEX_HOME`, etc.) — not tested here, both because it
would start background processes outside the scope of "one owned file, no side effects" and
because of the time budget. Probe: in a disposable sandbox, run
`CODEX_HOME=/tmp/codex-a codex login` and `CODEX_HOME=/tmp/codex-b codex login` against two
different ChatGPT accounts, then run one `codex` command per home concurrently and check for
socket/PID collisions.

## Summary table

| Question | Answer | Confidence |
|---|---|---|
| What identifies an account | `tokens.account_id` (+ `chatgpt_account_id`/`chatgpt_user_id`/`chatgpt_plan_type` claims) inside `auth.json`, scoped by `auth_mode` (`chatgpt` vs presumably `apikey`) | Measured |
| Where the credential lives | `~/.codex/auth.json`, mode 0600, flat object, one account per file | Measured |
| `[profiles.*]` = account or preset? | Preset only — no account/credential fields exist in `config.toml` on this machine; no `[profiles.*]` block present to inspect further | Measured (this machine) + UNKNOWN (other installs) |
| How a process binds to an account | `CODEX_HOME` env var selects the directory holding `auth.json`; unset here so it defaults to `~/.codex`; daemon/socket/lock files are singular per `CODEX_HOME` | Measured, with UNKNOWN on full env-var precedence list |
| Can two accounts coexist in one process/daemon | No — `auth.json` and the daemon control files are both singular per `CODEX_HOME`; this repo's existing wrapper (`bin/codex-p2p-attach`) does not vary `CODEX_HOME` today | Measured (structure) + UNKNOWN (untested side-by-side launch) |

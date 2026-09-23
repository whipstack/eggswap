# Is a Codex OS-keyring credential entry namespaced per CODEX_HOME, or global?

Measured 2026-09-23 against `codex-cli 0.156.1`, binary resolved via
`readlink -f ~/.local/bin/codex` -> `~/.codex/packages/standalone/releases/0.156.1-aarch64-apple-darwin/bin/codex`.
Builds on `codex-account-model.md` (CODEX_HOME is the binder; `auth.json` is a flat
single-account object). Read-only throughout: no `codex login`/`logout`, no keychain
write/delete, `~/.codex/auth.json` untouched.

## Why this matters

eggswap treats `CODEX_HOME` as the account boundary. That only holds while the credential
store is file-backed (`auth.json` inside that directory). If the OS keyring entry Codex
uses is a single global `(service, account)` pair instead of one pair per `CODEX_HOME`,
then two `CODEX_HOME` directories on a keyring-backed install are two *views* of the same
stored secret, not two accounts — a rotator that "switches" between them would just be
re-reading/re-writing one shared keychain item while believing it had isolated capacity.

## What this machine actually runs

`cli_auth_credentials_store = "file"` is the literal default baked into the binary
(found twice in `strings`, at two separate TOML-schema dump sites — evidence it's a real
config key with a documented default, not a guess). This machine's `~/.codex/config.toml`
sets no override, and `auth.json` carries all four token fields directly. There is **no
`~/.codex/secrets/` directory on this machine** (`ls -la ~/.codex/` shows only `packages/`
at top level besides the known files) — confirming the keyring path has never been
exercised here. `security find-generic-password -s codex -a codex` returned nothing, and
`security dump-keychain 2>/dev/null | grep -i codex` returned nothing: there is no live
Codex keychain item to inspect directly on this machine. The question has to be answered
from the binary, not from live keychain state.

## What the binary's string table shows

`strings <binary>` on the 0.156.1 release binary (Rust binaries pack adjacent `&str`
literals with no separating NUL, so `strings` merges neighboring literals into one run —
segment boundaries below are inferred from known field/message shapes, not guaranteed).

Two distinct keyring-touching code paths exist:

1. **A generic OAuth-token keyring path** (older/general), logged via `keyring` crate
   3.6.3 directly (`.../keyring-3.6.3/src/lib.rs` appears in the string table). Structured
   tracing lines:
   ```
   keyring.load start, service=, account=
   keyring.load success, service=, account=
   keyring.load no entry, service=, account=
   keyring.load error, service=, account=, error=
   keyring.save start, service=, account=, value_len=
   keyring.save success, service=, account=
   keyring.save error, service=, account=, error=
   keyring.delete start, service=, account=
   keyring.delete success, service=, account=
   keyring.delete no entry, service=, account=
   keyring.delete error, service=, account=, error=
   ```
   plus the four brief-cited messages (`failed to read/write/delete OAuth tokens from
   keyring`, `failed to load/save/delete CLI auth from keyring`). These are `tracing::info!`
   format strings — the **field names** (`service=`, `account=`) are compiled literals, but
   the **values** are runtime-interpolated and never appear as static strings. `strings`
   cannot show us what's actually passed as `service` or `account` here.

2. **A newer `codex_secrets::local` module** (`secrets/src/local.rs`, confirmed by an
   `event secrets/src/local.rs:140` tracing-instrument site) that manages age-encrypted
   files, not raw OAuth tokens, in the keyring. The literal run around that site is:
   ```
   secrets · local.age · codex_auth.age · mcp_oauth.age · gateway_oauth.age · codex ·
   failed to persist secrets key in keyring · secret value must not be empty ·
   event secrets/src/local.rs:140 · codex_secrets::local
   ```
   Read as: this module encrypts several logical stores (`local.age`, `codex_auth.age`,
   `mcp_oauth.age`, `gateway_oauth.age`) with a symmetric key, and that key itself is what
   gets persisted to the OS keyring on `keyring` mode (`failed to persist secrets key in
   keyring`). The bare literal `codex` sitting immediately before that error message, with
   no other candidate literal nearby, is the best fit in the string table for the keyring
   **service** name used for this key.

No path, hash, or `CODEX_HOME`-derived literal appears fused into that same run. Also
checked whether `~/.codex/installation_id` (36 bytes, present *inside* this `CODEX_HOME`,
confirmed via `ls -la`) is wired into the keyring/secrets code: `installation_id` appears
**only** elsewhere in the string table, as a telemetry field name and the `x-codex-
installation-id` HTTP header used for analytics — never co-located with the `keyring.*` or
`codex_secrets::local` strings. That's evidence against `installation_id` being the source
of a per-home keyring account/service value, though it doesn't rule out some other runtime
value (a hash of the `CODEX_HOME` path, computed and never spelled out as a string literal)
being used for the `account` half of the pair.

## Verdict

**STRONGLY SUGGESTED, not OBSERVED:** the keyring *service* name for the secrets-key entry
is the constant literal `"codex"` — a single global value with no visible per-`CODEX_HOME`
component in the binary's string table. `codex_secrets::local` names its encrypted files
generically (`codex_auth.age`, etc.), consistent with one key protecting one home's files,
but the SERVICE identifier used to look that key up in the OS keyring does not appear to
vary.

**Still UNKNOWN:** the *account* half of the `(service, account)` pair. `strings` shows the
log field name `account=` but never its value — that string is built at runtime and isn't
present in the binary as a literal. If `account` is also constant (e.g. `"codex"` or a
fixed username), the whole entry is global and two `CODEX_HOME`s on a keyring-backed
install collide on one secret. If `account` is derived from something that lives inside
`CODEX_HOME` (a path, a per-home generated id), the entry is namespaced. The binary alone
cannot settle this.

## The one experiment that would settle it

Do **not** run this against the operator's real accounts — it needs two disposable ChatGPT
test logins. On a machine with a real keychain backend (this Mac qualifies):

1. `export CODEX_HOME=/tmp/codex_home_A && mkdir -p "$CODEX_HOME"`, set
   `cli_auth_credentials_store = "keyring"` in `$CODEX_HOME/config.toml`, run `codex login`
   against test account A.
2. Immediately after, run `security dump-keychain 2>/dev/null | grep -B2 -A6 -i codex`
   (or `security find-generic-password -s codex -g 2>&1`, which prints the `acct`
   attribute without the secret) and record the exact `svce`/`acct` attribute values.
3. `export CODEX_HOME=/tmp/codex_home_B && mkdir -p "$CODEX_HOME"`, same
   `cli_auth_credentials_store = "keyring"`, `codex login` against a *different* test
   account B.
4. Re-run the same `security` query. Two outcomes are distinguishable and conclusive:
   - **One keychain item total**, and/or home A's `auth.json`-equivalent state now
     resolves to account B's identity when read back under `CODEX_HOME=/tmp/codex_home_A`
     → the entry is **global**: eggswap's per-`CODEX_HOME` account model is unsound on a
     keyring-backed install.
   - **Two distinct keychain items** (different `acct` values, or the same `svce`/`acct`
     but Codex still reports the correct, different account when queried under each
     `CODEX_HOME`) → the entry is **namespaced**, and eggswap's model holds.
5. Clean up both test items and directories afterward; this is disposable state on
   disposable test accounts, not the operator's credentials.

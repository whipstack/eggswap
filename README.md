# eggswap

eggswap is one local, single-owner, multi-account plane over BOTH Claude and
Codex accounts, so one person's tools stop idling on an exhausted account
while a second, perfectly usable account sits unused. It does not pool
accounts across different people — it is a scheduler over accounts you
already own, not a sharing mechanism. It is provider-neutral by design: a
caller asks "which account, of any provider, should take this unit of work"
and gets one answer.

**What "rotates" does and does not mean here.** eggswap has no background
daemon and no cross-call counter. `select` is a pure function: each call
ranks the accounts that are schedulable *right now* and returns the best one.
Rotation is a consequence of two facts rather than a timer — an exhausted or
unreadable account is never returned, and an account currently held by a
running `eggswap run` is excluded while it is held. So consecutive commands
spread across your accounts, but nothing watches a running process and
migrates it when its account hits a wall. If you want that, you have to call
eggswap again.

**One directory is not always one account, on the Codex side.** eggswap
treats `CODEX_HOME` as the account boundary, which is true where the
credential is file-backed — the common case, and the one this was built
against. But Codex supports `cli_auth_credentials_store` = `file` |
`keyring` | `auto` | `ephemeral`, and an admin policy can override your
config. On a keyring-backed install the credential does not live under
`CODEX_HOME`, so two directories are two views of possibly one account.
eggswap measures the store per profile and reports it in the profile's
metadata rather than assuming isolation: a home whose `auth.json` carries no
tokens reads as `unknown`, never as `file`, even if the config claims
otherwise. It checks user config, system managed settings and macOS MDM
requirements; unreadable or conflicting settings also read as `unknown`. In a
multi-home setup, any non-file-backed or unknown profile is unschedulable.
Whether a keyring entry is namespaced per `CODEX_HOME` is **untested**. A
single keyring-backed profile can still use its capacity reader, but do not
assume multiple such homes are separate accounts until isolation is verified.

**The two providers are not at parity today.** The Claude side reads every
account `cswap` knows about, with 5h/7d and any scoped windows, from one
local call. The Codex side reads as many accounts as you have `CODEX_HOME`
directories, and gets a capacity number only from a live app-server call —
one bucket, and only one populated on the account this was built against.
See "State of the project" below before assuming symmetry.

## The honesty contract

This is what distinguishes eggswap from a script that shells out and hopes:

- **Unknown capacity is `Unknown`, with an age — never zero, never
  unlimited.** A transport failure while asking about an account says
  nothing about that account's actual quota. See `UNKNOWN_IS_NOT_ZERO` in
  `eggswap/core/types.py`.
- **A frozen percentage never renders as fresh.** Every quota number carries
  its `observed_at` time; a stale read renders as `UNKNOWN (last read Ns
  ago, stale)`, not as the last good value repainted with today's date.
- **API-key profiles are OFF by default.** They are never auto-selected
  without an explicit opt-in and a budget — no silent paid fallback.
- **A dead credential is not the same as a full quota.** `AuthDead` (needs a
  human to log in again) is a distinct state from `Exhausted` (heals with
  time); eggswap never promises to autonomously recover access it no longer
  has.

## Install

```
pip install -e .
```

Requires Python >= 3.10. Zero runtime dependencies — everything eggswap
does at runtime is Python's standard library.

Supported operating systems are macOS and Linux. The cross-process account
lease uses POSIX `fcntl.flock`; Windows is not supported yet.

## Commands

```
eggswap list      # every profile, both providers, with honest availability
eggswap status    # one-line summary of which profiles are schedulable
eggswap select    # print the chosen profile for the next unit of work; does not launch it
eggswap run       # bind a process to the selected profile's account and launch it
```

`select` and `run` are deliberately separate: selecting a candidate never
reserves it, and only `run` takes the exclusive hold. See `Candidate` vs
`Lease` in `eggswap/core/types.py`.

That hold is real and it is the reason this project has a `Lease` type at
all. Two CLIs sharing one account's config directory concurrently rotate a
single-use refresh token and destroy it, and the account then needs a manual
re-login. So `eggswap run` acquires a fenced, per-account lease before it
launches anything and releases it when the child exits; a second `run`
against a held account refuses with exit code 10 and names the holder rather
than starting a second process. An expired hold is reaped automatically, so a
crashed run does not fence an account off until someone deletes a lock file
by hand. `status` lists held accounts separately from schedulable ones.

## Non-goals

eggswap explicitly does **not**:

- Reimplement OAuth, a keychain, or token refresh. Claude account binding is
  delegated to `cswap run` (which sets `CLAUDE_CONFIG_DIR`); Codex binding
  is the `CODEX_HOME` directory eggswap points a process at.
- Hold your tokens. No value in this codebase is ever a credential; an
  account is named only by an opaque id.
- Act as a credential pool. It schedules among accounts one owner already
  has, on one machine.
- Support sharing accounts between different people. This is a
  single-owner tool.

## State of the project

**Claude** has a real, observed capacity signal today: quota is read through
the account's own session state and rendered with the honesty contract
above.

**Codex capacity is implemented, with one live account observation.** The
adapter starts a private `codex app-server` child in the selected profile's
`CODEX_HOME`, reads `account/rateLimits/read`, and stamps the response at
arrival because the response has no capture-time field. One live read on
codex-cli 0.156.1 observed the `codex:primary` window; the redacted transcript
and field-level limits are in
[`docs/research/eggswap/codex-ratelimits-live.md`](docs/research/eggswap/codex-ratelimits-live.md). That
single sample is evidence for the reader's schema, not current capacity or
proof of multiple-account isolation. The populated `secondary` bucket,
additional `limitId` values, and a distinct `account/rateLimits/updated`
push remain unverified. Missing or stale readings stay `Unknown`.

## License

MIT. See `LICENSE`. See `NOTICE` for third-party design attribution.

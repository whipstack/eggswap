# eggswap

One local tool to choose and run work on your own Claude and Codex accounts.
It reads Claude accounts from `cswap` and Codex accounts from separate
`CODEX_HOME` directories. It does not store or copy credentials.

## Install

Requires Python 3.10+, `cswap` for Claude, and the `codex` CLI for Codex.
Install the published wheel (Eggswap is not published to PyPI):

```sh
python3 -m pip install https://github.com/whipstack/eggswap/releases/download/v0.1.1/eggswap-0.1.1-py3-none-any.whl
eggswap status
```

Or, from a checkout: `python3 -m pip install -e .`. Runtime dependencies are
stdlib only; supported platforms are macOS and Linux.

## Add two Claude and two Codex accounts

### Claude: add accounts to cswap

```sh
cswap list
```

If your two accounts are already listed with live usage, they are already in
Eggswap. Their profile keys are `claude:1`, `claude:2`, etc. To register a new
Claude account, run:

```sh
eggswap login claude
cswap list
```

Repeat for the second account. Eggswap runs `claude auth login`, then `cswap
add`; the native tools keep the credentials. Do not invoke it from a `cswap
run` session. If a listed account says `re-login needed`, sign in to that
same account with `eggswap login claude` to refresh its slot. This changes the
default Claude login, so finish or move any process using it first. Work
launches still use `cswap run`, without a global `cswap switch` loop.

### Codex: sign in once per home

The first account can use the existing `~/.codex` login. Sign in to a
different account through Eggswap, naming its separate home:

```sh
eggswap login codex --home "$HOME/.local/share/eggswap/codex-2"
eggswap list
```

Complete the native Codex browser login with a *different* account. Eggswap
creates the home with a file-backed credential-store setting if it is new,
delegates authentication to `codex login`, and remembers only the home path.
It never handles the credential. For a device-code flow, add `--device-auth`.
Do not copy `auth.json` between homes. Existing `EGGSWAP_CODEX_HOMES` (paths
separated by `:` on macOS/Linux) and `CODEX_HOME` remain supported; the
enrolled home is remembered across shells without an environment variable.

**Check the result:** `eggswap list` should show two different `codex:<id>`
keys, each with quota data. A second directory alone does not prove account
isolation: Codex also supports keyring, auto and ephemeral credential stores,
and managed policy can override `config.toml`. With multiple homes Eggswap
reads Codex's effective settings and leaves capacity `UNKNOWN` unless it can
verify the `file` store. If either Codex profile is `UNKNOWN`, read its reason
in `eggswap list`; do not treat it as available. See
[Codex credential isolation](docs/codex-keyring-namespacing.md).

### Test all four without spending model quota

```sh
eggswap status
eggswap select --pin claude:1
eggswap select --pin claude:2
eggswap select --pin codex:FIRST_ID
eggswap select --pin codex:SECOND_ID
eggswap run --dry-run claude:1 -- --version
eggswap run --dry-run codex:SECOND_ID -- codex --version
```

Replace keys with the ones printed by `eggswap list`. `--pin` refuses an
unavailable account (exit 3); it never silently picks another. `--dry-run`
prints the launch binding and does not take a lease or start the provider CLI.
For a real launch and lease check, use `eggswap run claude:1 -- --version`
or `eggswap run codex:ACCOUNT_ID -- codex --version`. These version commands do not
send a model request. A second `run` against an account already held by an
Eggswap process exits 10. An unheld account can run independently.

If you have more than two Claude accounts, use `--pin` for this test. To keep
an account out of future automatic selection, use
`eggswap disable claude:SLOT` and later `eggswap enable claude:SLOT`.
Disabling persists across inventory refreshes and does not terminate a
running process.

## Commands

| Command | Purpose |
| --- | --- |
| `eggswap list` | Show every discovered account and its availability. |
| `eggswap status` | Show schedulable, held and disabled profiles. |
| `eggswap login claude` / `eggswap login codex --home PATH` | Authenticate through the provider's CLI; Codex homes are enrolled without storing tokens. |
| `eggswap select [--provider claude\|codex] [--explain]` | Choose a profile without reserving it; explain shows refusals. |
| `eggswap select --pin <key>` | Require one profile, or refuse. |
| `eggswap run <key> -- <command>` | Recheck capacity, take an exclusive lease and launch. For Claude, `<command>` is the argument list forwarded to `claude` through `cswap run`; for Codex, include the `codex` executable. |
| `eggswap run --dry-run <key> -- <command>` | Print the binding without launching. |
| `eggswap disable <key>` / `enable <key>` | Persistently exclude or restore a profile. |
| `eggswap clear <key>` | Clear a hand-held quarantine after its cause is fixed. |

`EGGSWAP_STATE_DIR` changes the local lease and quarantine directory (default
`~/.local/state/eggswap`). `EGGSWAP_CODEX_HOMES` is a path-separated list of
Codex homes. `CODEX_HOME` identifies a Codex home for the current shell.

Selection checks live quota and freshness. Unknown capacity, exhausted
quota and dead authentication are separate states; none is silently treated
as available. API-key accounts are off by default: there is no paid fallback.
`select` does not reserve an account; only `run` takes a fenced lease, renews
it while its child runs, and releases it afterward. Eggswap does not migrate
an already running process when quota changes. The default cross-provider
ordering is `provider_order`; `--cross-provider` also supports
`most_absolute_headroom`, `longest_until_reset` and `spread`.

Codex capacity comes from `codex app-server`'s `account/rateLimits/read`.
Missing or stale readings remain `UNKNOWN`. See [Codex capacity
measurement](docs/codex-ratelimits-live.md) and [quota bucket
semantics](docs/codex-ratelimit-multibucket.md).

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

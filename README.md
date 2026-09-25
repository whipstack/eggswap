# eggswap

One local tool to choose and run work on your own Claude and Codex accounts.
It reads Claude accounts from `cswap` and Codex accounts from separate
`CODEX_HOME` directories. It does not store or copy credentials.

## Start here

Requires Python 3.10+, `cswap` for Claude, and the `codex` CLI for Codex.
Install the published wheel (Eggswap is not published to PyPI):

```sh
python3 -m pip install https://github.com/whipstack/eggswap/releases/download/v0.1.1/eggswap-0.1.1-py3-none-any.whl
eggswap status
```

Or, from a checkout: `python3 -m pip install -e .`. Runtime dependencies are
stdlib only; supported platforms are macOS and Linux.
Add two accounts from each provider in your own terminal. Finish each browser
login before starting the next one, and choose a different account each time:

```sh
eggswap add --claude
eggswap add --claude
eggswap add --codex
eggswap add --codex
eggswap list
eggswap status
```

Skip an `add` if that account is already in `eggswap list`. Each successful
command prints its account key. Eggswap opens the provider's own login; you
complete it in the browser. Eggswap never asks for a token. The published
v0.1.1 wheel supports `--claude` and `--codex`; the no-flag `eggswap add`
menu and `eggswap --version` are in the next release.

`eggswap list` should show two distinct `claude:N` keys and two distinct
`codex:<id>` keys. `eggswap status` shows which can run work now. A new
account can appear in the list with `UNKNOWN` capacity until the provider
reports usable quota. For Codex, separate home directories alone do not prove
credential isolation; an unverified credential store also reports `UNKNOWN`.

To check a particular profile without launching work, copy its exact key from
`eggswap list` and run:

```sh
eggswap select --pin 'claude:1'
eggswap run --dry-run 'claude:1' -- --version
```

Replace `claude:1` with each of your four keys. `select --pin` refuses an
unavailable profile; `run --dry-run` shows its launch without starting work.
Check the human accounts in `cswap list` and the Codex IDs in `eggswap list`.

### If sign-in does not work

- `eggswap add --claude` runs `claude auth login`, then `cswap add`. Run it in a
  normal terminal, outside `cswap run`, with `CLAUDE_CONFIG_DIR` and
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` unset. It changes the default Claude login;
  finish other processes using that login first. Do not use `/logout` to add
  the next account. If the reported slot changes, check `cswap list` again.
- `eggswap add --codex` picks a new private home under
  `~/.local/share/eggswap/`. Use `--home /absolute/path` to choose one. If the
  browser callback cannot reach your CLI, retry with `--device-auth` and enter
  the code in your own terminal/browser. Never share a device code or
  `auth.json`.
- If the second login is the same account, sign in again with the other
  account. Eggswap refuses a duplicate Codex account ID. For Codex credential
  store details, see [isolation notes](docs/codex-keyring-namespacing.md).
- `eggswap list` names the reason for `UNKNOWN` or dead authentication. A
  previously quarantined Claude slot requires identity verification in
  `cswap list` before `eggswap clear claude:N`.

## Commands

| Command | Purpose |
| --- | --- |
| `eggswap list` | Show every discovered account and its availability. |
| `eggswap status` | Show schedulable, held and disabled profiles. |
| `eggswap add --claude` / `eggswap add --codex` | Authenticate through the provider's CLI; Codex homes are chosen automatically and enrolled without storing tokens. |
| `eggswap select [--provider claude\|codex] [--explain]` | Choose a profile without reserving it; explain shows refusals. |
| `eggswap select --pin <key>` | Require one profile, or refuse. |
| `eggswap run <key> -- <command>` | Recheck capacity, take an exclusive lease and launch. For Claude, `<command>` is the argument list forwarded to `claude` through `cswap run`; for Codex, include the `codex` executable. |
| `eggswap run --dry-run <key> -- <command>` | Print the binding without launching. |
| `eggswap disable <key>` / `eggswap enable <key>` | Persistently exclude or restore a profile. |
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

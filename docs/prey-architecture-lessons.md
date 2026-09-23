# Prey architecture lessons: which structural decisions survive a second provider

Source: `$HOME/.local/share/uv/tools/claude-swap/lib/python3.14/site-packages/claude_swap/`
(installed MIT package, read-only, no clone/network). Builds on
`docs/research/eggswap/prey-rotation-model.md` (rotation/quota mechanics) — this doc does not
repeat that content; it asks the harder question: what breaks when a second provider (Codex)
is added to a design built for exactly one.

## 1. Module map: concentrated vs. smeared

| Module | Owns | Claude-specificity |
|---|---|---|
| `paths.py` (211L) | `CLAUDE_CONFIG_DIR`, `~/.claude.json`, backup root | **Concentrated** — every path helper is named `get_claude_*` (`paths.py:34,42,56,66`); the env var and the `.claude.json` asymmetry (`paths.py:8-9`) are the only Claude facts in the file. |
| `session.py` (1448L) | `cswap run` launch, `CLAUDE_CONFIG_DIR` isolation, keychain service naming, live-session scan | **Smeared.** `CLAUDE_CONFIG_DIR` is set in three unrelated places (`session.py:490` docstring reference, `session.py:571` in `run()`, `session.py:1178-1182` guarding `.claude.json` writes), and `keychain_service_name()` (`session.py:232`) re-derives *Claude Code's own* hash of that env var (`session.py:235`) — a second provider does not share this env var or this hash scheme, so this logic cannot be parameterized, only duplicated. |
| `credentials.py` (1849L) | keychain routing, `.enc` backup files, atomic writes, backup/keychain reconciliation | **Smeared worst of all.** `CredentialStore` (`credentials.py:293`) is a leaf that *is* provider-agnostic in isolation (see §2), but the platform/backend matrix it encodes (macOS Keychain vs `.enc` files vs Windows keyring, `credentials.py:1043-1247`) is Claude Code's own storage behavior, learned empirically (issue #66, the `.enc`-wins rule at `credentials.py:1167-1169`) — that empirical model has no reason to hold for Codex's `CODEX_HOME` layout. |
| `oauth.py` (770L) | usage fetch, window parsing, headroom math | **Concentrated at the top, smeared at the bottom.** The HTTP call (`oauth.py:366,584`) and the endpoint (`api.anthropic.com/api/oauth/usage`) are a single hardcoded site — genuinely swappable. But `relevant_windows()` (`oauth.py:505-539`) and `account_headroom()` (`oauth.py:543-561`) bake in Anthropic's window vocabulary (`five_hour`, `seven_day`, scoped Fable windows) as the *shape* every downstream consumer reads, per the sibling doc §2 — that shape assumption is smeared into every caller that does `100.0 - max(pcts)`. |
| `autoswitch.py` (2364L) | poll loop, threshold gate, ranking, quarantine | **Concentrated but single-tenant.** `AutoSwitchEngine` (`autoswitch.py:633`) is one loop over one account list with one `strategy` setting (`settings.py:50`) — there is no seam for "run this loop per-provider" or "rank across two providers with different headroom semantics"; `_headroom_by_account()` (`autoswitch.py:621`) assumes every candidate's headroom is comparable on the same 0-100 scale, which stops being true the moment Codex's rate-limit shape differs from Anthropic's. |
| `settings.py` (488L) | config schema, `[50.0, 99.9]` threshold clamp, load/save | **Concentrated, provider-blind by omission** — no `provider` field exists anywhere in `AutoSwitchSettings` (`settings.py:33-61`); a second provider needs a new dimension in the schema, not a new value in an existing one. |
| `process_detection.py` (256L) | PID+start-time liveness (`process_detection.py:109-132`), session scan | **Concentrated and reusable as-is** — `is_claude_process_identity_alive()` checks a PID against `ps` output; the *concept* (verify identity, not just PID) transfers directly to a Codex process, only the `claude` binary name is provider-specific. |

The expensive smear is `CLAUDE_CONFIG_DIR`-as-identity: it is simultaneously the process
isolation mechanism (`session.py:571`), the keychain lookup key (`session.py:232-246`), and the
thing three other modules read to detect "am I already inside a managed session"
(`session.py:529-535`, `credentials.py:48-69`). One environment variable plays three structural
roles at once, so a second provider can't reuse any one of those roles without either inventing
its own three-role variable or breaking the assumption that config-dir-identity is singular.

## 2. Seam test

| Concern | Seam or hardcoded? | Evidence |
|---|---|---|
| **Account identity** | **Hardcoded shape, not a seam.** `AccountInfo` (`models.py:79-120`) has `organization_uuid`/`organization_name` as required identity fields, and `from_dict`/`to_dict` (`models.py:100-120`) map directly to Anthropic's OAuth-profile JSON keys. Nothing abstracts "what makes an account identity" — the dataclass *is* Anthropic's payload shape. |
| **Credential storage** | **Genuine seam, underused.** `_StoreHost(Protocol)` (`credentials.py:280-293`) is exactly the right abstraction — `CredentialStore` takes a host view, not a concrete switcher, per the sibling doc §1. But the Protocol's own methods (`credentials_dir`, keychain calls) are still typed around a single macOS-Keychain-or-`.enc`-file backend matrix (`credentials.py:1043-1247`); the seam exists at the *coupling* boundary (store ↔ switcher) but not at the *backend* boundary (Keychain/file ↔ some other provider's credential home). |
| **Capacity reading** | **Hardcoded call site, seamed data shape below it.** The fetch itself is one function, one URL (`oauth.py:584`, `api.anthropic.com/api/oauth/usage`) — trivially swappable per-provider. But `QuotaWindow`-equivalent parsing (`relevant_windows()`, `oauth.py:505-539`) assumes the Anthropic bucket vocabulary as noted in §1; a second provider needs its own parse function returning the *same normalized shape*, which the prey never had to define because it only ever had one shape. |
| **Selection policy** | **Hardcoded into the loop.** `_every_account_above_threshold()` (`autoswitch.py:609`) and the `"best"`/`"consume-first"` sort keys (`autoswitch.py:1946-1949`, per sibling doc §3) are inline comparisons inside `AutoSwitchEngine`, not a pluggable ranking function. There is no `Selector` interface to implement for a second provider's different headroom semantics. |
| **Process binding** | **Seam exists, is provider-shaped.** `execvpe(claude_bin, argv, env)` (`session.py:605`) is one call with the binary name and env dict as parameters — structurally swappable per provider. But everything that decides *what* env dict to build (`_probe_env`, `session.py:487-490`; the `CLAUDE_CONFIG_DIR` isolation contract) is Claude-specific per §1, so the seam is real at the exec call but the thing feeding it is not. |

Net: the prey has exactly one clean seam (`_StoreHost` Protocol) and four places where the
Anthropic shape is load-bearing all the way down to the call site.

## 3. Copy this one; do not copy that one

**Copy:** the `_StoreHost` Protocol pattern (`credentials.py:280-293`) — a leaf collaborator
that receives a narrow read-only view of its host instead of importing the orchestrator, so
storage logic can be tested and reasoned about without the switcher, and so a second backend
implementation only has to satisfy the Protocol, not inherit from anything. This is the one
piece of the prey that was already built for substitutability, even though it was only ever
substituted within one provider.

**Do not copy:** the single-shape `AccountInfo`/`QuotaWindow`-equivalent-data model — treating
"account identity" and "capacity reading" as one Anthropic-JSON-shaped dataclass each
(`models.py:79-120`, `oauth.py:505-539`) rather than a provider-neutral interface with
per-provider adapters underneath. This is precisely why the smear in §1 exists: because there
was only ever one shape, nothing forced a boundary between "what an account IS" and "what
Anthropic's OAuth payload happens to contain". eggswap's `core/types.py` already avoids this
(`Profile`/`QuotaWindow` are provider-neutral, `Provider` is an attribute not a branch,
per its own docstring) — this finding is the concrete argument for why that design choice in
`core/types.py` was correct, not a nice-to-have.

## 4. Edge cases a naive reimplementation would forget

- **Partial/interrupted writes.** Every persisted file (usage cache, credential backups, the
  migration state file, settings) is written via `mkstemp` + `os.replace` (`fsutil.py:72-91`,
  `credentials.py:734,757,1123`, `settings.py:443`), never opened for in-place write. `_replace`
  is retried past transient Windows sharing failures (`fsutil.py:72-91`) — POSIX rename is atomic
  but Windows can transiently fail the same call, and the prey learned that empirically.
- **`Path.exists()` lies on some Python versions.** `credentials.py:1172-1194` deliberately uses
  `.stat()` instead of `.exists()` because `Path.exists()` swallows `OSError` on Python 3.13+ and
  answers `False` for an unsearchable (not just absent) directory — collapsing "permission denied"
  into "genuinely missing" on newer interpreters only. A fleet running mixed 3.12/3.14 got
  inconsistent failure semantics until this was special-cased.
- **Corrupt vs. missing vs. unreadable, kept as three distinct outcomes**, not folded into one
  "no value" branch: `FileNotFoundError` → absent; `OSError` on a present file → real failure,
  reported to the caller's `failed` list (`credentials.py:1184-1194,1200-1209`); base64-decode
  failure on a present, readable file → corrupt content, falls through to the other backend
  *without* being marked as a read failure (`credentials.py:1216-1220`). Three different recoveries
  for what a less careful implementation would treat as one `None`.
- **Empty-but-present file is not a valid backup.** A whitespace-only `.enc` is explicitly
  distinguished from "has content" (`credentials.py:1222-1224`) and treated as absent, not as
  corrupt and not as valid — otherwise a truncated write mid-crash could silently shadow a good
  Keychain copy forever.
- **Concurrent same-account launch is a known, accepted gap, not an oversight** — per the sibling
  doc §4, `run()` holds no lock across the `execvpe` call by design (the lock must not be inherited
  by the exec'd process, `session.py:593-594`), and the only mitigation is a same-account fast-path
  that skips creating a second profile copy, justified by refresh-token drift risk
  (`session.py:541-542`), not by mutual exclusion. A naive port would either add a lock that breaks
  the exec handoff or skip the fast-path and hit the drift bug it was added to avoid.
- **PID reuse**, not just PID liveness: `is_claude_process_identity_alive()`
  (`process_detection.py:109-132`) cross-checks the recorded process start time against `ps`
  output, because a bare `kill(pid, 0)` check would return "alive" for an unrelated process that
  reused a dead session's PID.
- **A corrupt migration-state file can never permanently block a migration** — `_read_raw` for
  `.migrations.json` returns `{}` on any parse failure (`migrations.py:57-60`) rather than raising,
  because in this file specifically, treating "state unknown" as "state empty" is safe (migrations
  are idempotent) — note this is the *opposite* default from `core/types.py`'s `Unknown`-is-never-
  zero rule, and the prey is correct to differ here only because re-running an idempotent migration
  is safe while re-scheduling onto a dead account is not. eggswap must not copy this pattern into
  quota reading by reflex.
- **First-run / no-managed-accounts-yet is a distinct branch**, not an empty-list fallthrough:
  `migrate_windows_keyring_to_files` explicitly returns early ("No managed accounts yet — let a
  later restore migrate", `migrations.py:151`) rather than attempting a migration against nothing.
- **An inaccessible backend during migration is not "nothing to migrate.·"** — a `keyring` import
  or backend failure during the Windows migration path is deferred and logged, not treated as
  success (`migrations.py:164-171`); conflating "could not check" with "checked, found nothing" is
  exactly the Unknown-vs-zero mistake `core/types.py` was written to prevent, and the prey avoids
  it here too even without a type system enforcing it.
- **Clock discipline**: lock wait timeouts use `time.monotonic()` (`locking.py:55`), never wall
  clock, so an NTP step or DST transition can't corrupt a lock-acquire timeout — but *freshness*
  bookkeeping (`fetchedAt`, `age_s`) uses epoch seconds (`usage_store.py:1073`, sibling doc §2)
  because that value has to be compared against a server-supplied `resets_at`, which is also epoch.
  The prey picks the clock source per what it's being compared against, not uniformly.

## What this means for eggswap

`core/types.py`'s sealed `Availability` union and `Profile.key = f"{provider}:{account_id}"`
already dodge the two structural failures found here: the single-shape identity dataclass (§1,
§3) and the "unknown collapses to a default" instinct that shows up in one place even in the
prey's own code (§4, migration state). The concrete risk `core/types.py` does not yet resolve by
itself is the **selection-policy hardcoding** in §2: eggswap needs an actual per-provider ranking
seam — Candidate/score already model the *output* of ranking, but nothing yet defines how two
providers' differently-shaped Availability get compared on one score axis, which is exactly the
gap `_headroom_by_account`'s single 0-100 scale papers over in the prey.

"""eggswap CLI -- the user-facing command over the provider-neutral contract.

WHY THIS MODULE RENDERS THE WAY IT DOES
----------------------------------------
This is the surface a human actually reads, so it is where the negative-cache
incident (eggswap/core/types.py) either gets re-introduced or stays fixed. A
stale ``Unknown``/frozen ``QuotaWindow`` must never print as a fresh number,
and ``AuthDead`` (heals only via human re-login) must never look like
``Exhausted`` (heals with time) -- conflating either pair here would silently
recreate the four-workers-dispatched-onto-a-dead-account incident one layer
up, in the one place a human is actually looking.

``main(argv, *, adapters=None, out=sys.stdout)`` takes adapters as a
parameter (never imports a global registry) so tests drive it in-process with
fake adapters -- no network, no real account, no subprocess -- per this
estate's determinism rule.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from eggswap.core.select import Policy, select as select_candidate
from eggswap.core.types import (
    Available,
    AuthDead,
    Candidate,
    Exhausted,
    NoCapacity,
    Profile,
    Provider,
    Unknown,
    LeaseError,
    StaleFence,
)
from eggswap.core.quarantine import AUTH_DEAD, Failure, INDEFINITE, Quarantine, UNKNOWN

__all__ = ["main"]

DEFAULT_MAX_AGE_SECONDS = 300.0


def default_lease_root() -> Path:
    """Where the exclusive holds live.

    Outside the repository and outside any provider's config directory: a
    lease is machine state, it outlives a checkout, and it must never land
    somewhere a `cswap` or `codex` operation could clobber.
    """
    root = os.environ.get("EGGSWAP_STATE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state" / "eggswap"
    return base / "leases"


def default_quarantine_path() -> Path:
    """Where the failure memory lives -- same state root as leases, same
    ``EGGSWAP_STATE_DIR`` override, one file rather than one-per-profile
    because the whole record is small and a single atomic replace is enough.
    """
    root = os.environ.get("EGGSWAP_STATE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".local" / "state" / "eggswap"
    return base / "quarantine.json"


def _quarantine_to_dict(quarantine: Quarantine) -> dict:
    return {
        "failures": {
            key: {"kind": f.kind, "at": f.at, "detail": f.detail}
            for key, f in quarantine._failures.items()
        },
        "until": dict(quarantine._until),
        "counts": {
            f"{key}\x1f{kind}": count
            for (key, kind), count in quarantine._counts.items()
        },
    }


def _quarantine_from_dict(data: dict, *, clock) -> Quarantine:
    quarantine = Quarantine(clock=clock)
    for key, raw in data.get("failures", {}).items():
        quarantine._failures[key] = Failure(
            profile_key=key, kind=raw["kind"], at=raw["at"], detail=raw.get("detail", "")
        )
    for key, until in data.get("until", {}).items():
        quarantine._until[key] = until
    for combined, count in data.get("counts", {}).items():
        key, kind = combined.split("\x1f", 1)
        quarantine._counts[(key, kind)] = count
    return quarantine


def load_quarantine(path: Path, *, clock=time.time) -> Quarantine:
    """Load the on-disk failure memory, degrading to EMPTY on any problem.

    Failing open here is deliberate, not an oversight: a quarantine we cannot
    read must never block a launch. The cost of a missed quarantine is one
    wasted launch attempt (the same launch that would have happened before
    this feature existed); the cost of crashing on a corrupt or unreadable
    file is no launch at all. An empty quarantine is exactly as safe as the
    world before this feature was wired in, so that is the failure floor.
    """
    try:
        text = path.read_text()
    except OSError:
        text = ""
    data: dict = {}
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
    try:
        return _quarantine_from_dict(data, clock=clock)
    except Exception:  # pragma: no cover - any malformed shape degrades to empty
        return Quarantine(clock=clock)


def save_quarantine(quarantine: Quarantine, path: Path) -> None:
    """Atomic commit: tmp + os.replace, matching LeaseStore's write discipline
    so a crash mid-write can never leave a torn quarantine file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f".tmp.{os.getpid()}")
    with open(tmp_path, "w") as f:
        json.dump(_quarantine_to_dict(quarantine), f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _held_keys(store, profiles, *, now: float) -> set:
    """Profile keys currently held by a LIVE lease.

    Expired holds are reaped first, so a crashed `eggswap run` cannot fence
    off an account forever -- the failure mode that makes people delete lock
    files by hand, which is how the guarantee gets lost.
    """
    if store is None:
        return set()
    try:
        store.reap_expired()
    except Exception:  # pragma: no cover - a broken store must not hide accounts
        return set()
    held = set()
    for profile in profiles:
        try:
            lease = store.holder_of(profile)
        except Exception:  # pragma: no cover
            continue
        if lease is not None and not lease.is_expired(now=now):
            held.add(profile.key)
    return held


def _default_codex_homes() -> List[Path]:
    """Which Codex accounts this machine can see.

    A Codex account IS a CODEX_HOME directory -- one home, one auth.json, one
    account, for the life of a process (measured on codex-cli 0.156.1; see
    docs/research/eggswap/codex-account-model.md). So enumerating accounts is
    enumerating directories, and there are exactly three honest sources:

      EGGSWAP_CODEX_HOMES   os.pathsep-separated, the multi-account case;
      CODEX_HOME            the single home this shell is already bound to;
      ~/.codex              the default home, when it actually exists.

    Deliberately NOT a filesystem scan for anything that looks like a home:
    guessing which directories are accounts is how a tool starts launching
    work against a profile its owner never enrolled. Order is preserved and
    duplicates are dropped, so an explicit list wins over the default.
    """
    homes: List[Path] = []
    seen = set()

    def add(raw: str) -> None:
        if not raw:
            return
        path = Path(raw).expanduser()
        key = str(path)
        if key not in seen and path.is_dir():
            seen.add(key)
            homes.append(path)

    for raw in (os.environ.get("EGGSWAP_CODEX_HOMES") or "").split(os.pathsep):
        add(raw.strip())
    add(os.environ.get("CODEX_HOME") or "")
    add(str(Path.home() / ".codex"))
    return homes


def _default_adapters() -> List[Any]:
    """Real adapters for interactive use. Never called by tests.

    The Codex adapter is wired to the live app-server rate-limit reader here
    and only here: the adapter itself defaults to no reader, so importing it
    can never imply a capacity claim. If the reader cannot be imported or the
    app-server call fails, the adapter reports UNKNOWN with the reason -- a
    Codex account with an unreadable quota is not a Codex account with quota.
    """
    from eggswap.adapters.claude_cswap import ClaudeCswapAdapter
    from eggswap.adapters.codex_home import CodexHomeAdapter

    homes = _default_codex_homes()
    reader = None
    if homes:
        try:
            from eggswap.adapters.codex_ratelimits import AppServerRateLimitReader

            reader = AppServerRateLimitReader(codex_home=homes[0])
        except Exception:  # pragma: no cover - import guard, not logic
            reader = None
    return [ClaudeCswapAdapter(), CodexHomeAdapter(homes, rate_limit_reader=reader)]


def _score(availability) -> float:
    """Headroom score for ranking. Only meaningful for Available -- everything
    else is ineligible in core.select.rank regardless of the number here.
    """
    if not isinstance(availability, Available) or not availability.windows:
        return 100.0
    return 100.0 - max(w.used_percent for w in availability.windows)


def _build_candidates(adapters: Sequence[Any], *, now: float, max_age_seconds: float) -> List[Candidate]:
    candidates: List[Candidate] = []
    for adapter in adapters:
        for profile in adapter.profiles():
            availability = adapter.availability(profile, max_age_seconds=max_age_seconds)
            candidates.append(
                Candidate(profile=profile, availability=availability, score=_score(availability))
            )
    return candidates


def _render_availability(availability, *, max_age_seconds: float, now: float) -> str:
    """One line, honest about staleness -- see module docstring.

    AuthDead and Exhausted are deliberately worded so neither can be mistaken
    for the other at a glance: only AuthDead ever says "re-login needed".
    """
    if isinstance(availability, Available):
        if not availability.windows:
            return "available (no quota data)"
        return "; ".join(w.render(max_age_seconds, now=now) for w in availability.windows)
    if isinstance(availability, Exhausted):
        reset = "unknown" if availability.reset_at is None else f"{availability.reset_at:.0f}"
        bucket = availability.bucket or "?"
        return f"EXHAUSTED ({bucket}, resets at {reset})"
    if isinstance(availability, AuthDead):
        return f"AUTH DEAD -- re-login needed ({availability.reason})" if availability.reason else "AUTH DEAD -- re-login needed"
    if isinstance(availability, Unknown):
        age = availability.age_seconds(now=now)
        return f"UNKNOWN (last read {age:.0f}s ago)"
    return f"UNKNOWN (unrecognized availability {type(availability).__name__!r})"  # pragma: no cover


def _enumeration_failures(adapters) -> list:
    """Providers whose account list could not be READ, as opposed to providers
    that genuinely have no accounts.

    Reporting these is the whole honesty contract applied one level up. A
    profile whose quota is unreadable renders UNKNOWN; a PROVIDER whose
    profiles are unreadable must not simply be missing from the output, or
    the tool answers "3 accounts" when the truthful answer is "3, and I could
    not see the rest".
    """
    failures = []
    for adapter in adapters:
        status = getattr(adapter, "enumeration_status", None)
        if status is None:
            continue
        try:
            reason = status()
        except Exception:  # pragma: no cover - a broken adapter is not a verdict
            reason = "enumeration_status() itself failed"
        if reason:
            failures.append((getattr(adapter, "provider", "?"), reason))
    return failures


def _warn_enumeration(adapters, out) -> None:
    for provider, reason in _enumeration_failures(adapters):
        print(f"{provider}: UNKNOWN -- account list unreadable: {reason}", file=out)


def _cmd_list(adapters, policy: Policy, out, *, now: float) -> int:
    candidates = _build_candidates(adapters, now=now, max_age_seconds=policy.max_age_seconds)
    _warn_enumeration(adapters, out)
    if not candidates:
        print("no profiles configured", file=out)
        return 0
    for candidate in candidates:
        line = _render_availability(candidate.availability, max_age_seconds=policy.max_age_seconds, now=now)
        print(f"{candidate.profile.key}\t{candidate.profile.label}\t{line}", file=out)
    return 0


def _quarantine_line(quarantine: Quarantine, key: str) -> str:
    """A profile being skipped must stay VISIBLE -- the same rule the held-
    profile line already applies one level up."""
    failure = quarantine.reason(key)
    until = quarantine.until(key)
    until_text = "indefinite" if until == INDEFINITE else f"{until:.0f}"
    kind = failure.kind if failure is not None else UNKNOWN
    return f"quarantined: {key} until {until_text} ({kind})"


def _cmd_status(adapters, policy: Policy, out, *, now: float, store=None, quarantine=None) -> int:
    candidates = _build_candidates(adapters, now=now, max_age_seconds=policy.max_age_seconds)
    _warn_enumeration(adapters, out)
    held = _held_keys(store, [c.profile for c in candidates], now=now)
    quarantined = set()
    if quarantine is not None:
        quarantined = {
            c.profile.key for c in candidates if quarantine.is_quarantined(c.profile.key)
        }
    schedulable = [
        c for c in candidates
        if c.availability.schedulable and c.profile.key not in held and c.profile.key not in quarantined
    ]
    print(
        f"{len(candidates)} profile(s), {len(schedulable)} schedulable: "
        + (", ".join(c.profile.key for c in schedulable) if schedulable else "none"),
        file=out,
    )
    if held:
        print("held: " + ", ".join(sorted(held)), file=out)
    for key in sorted(quarantined):
        print(_quarantine_line(quarantine, key), file=out)
    return 0 if schedulable else 3


def _cmd_select(adapters, policy: Policy, out, *, now: float, as_json: bool, store=None, quarantine=None, explain: bool = False) -> int:
    candidates = _build_candidates(adapters, now=now, max_age_seconds=policy.max_age_seconds)
    held = _held_keys(store, [c.profile for c in candidates], now=now)
    candidates = [c for c in candidates if c.profile.key not in held]
    if quarantine is not None:
        candidates = quarantine.filter(candidates)
    if explain:
        # The audit path. eggswap/core/decision.py records WHY a profile was
        # chosen and, more usefully, why each other one was refused -- and it
        # was reachable from nothing, which is the built-and-unwired shape
        # this project has already been caught in twice (the lease, then the
        # quarantine). A record a human cannot reach audits nothing.
        from eggswap.core.decision import decide

        record = decide(candidates, policy, now=now)
        if as_json:
            print(
                json.dumps(
                    {
                        "profile": record.chosen.profile.key if record.chosen else None,
                        "rationale": record.rationale,
                        "considered": list(record.considered),
                        "refused": [
                            {"profile": r.profile_key, "reason": r.reason}
                            for r in record.refused
                        ],
                        "policy": dict(record.policy_summary),
                    },
                    indent=2,
                ),
                file=out,
            )
        else:
            print(record.rationale, file=out)
            for refusal in record.refused:
                print(f"  refused {refusal.profile_key}: {refusal.reason}", file=out)
        return 0 if record.chosen is not None else 3

    try:
        chosen = select_candidate(candidates, policy, now=now)
    except NoCapacity as exc:
        # NoCapacity already renders its own "no schedulable profile: ..."
        # preamble with the per-profile reasons; prefixing it again produced
        # "no schedulable profile: no schedulable profile: claude:2=AuthDead".
        print(str(exc), file=out)
        return 3
    if as_json:
        print(
            json.dumps(
                {
                    "profile": chosen.profile.key,
                    "provider": chosen.profile.provider.value,
                    "score": chosen.score,
                }
            ),
            file=out,
        )
    else:
        print(chosen.profile.key, file=out)
    return 0


def _find_profile(adapters, profile_key: str):
    for adapter in adapters:
        for profile in adapter.profiles():
            if profile.key == profile_key:
                return adapter, profile
    return None, None


def _settle_quarantine(
    quarantine,
    quarantine_path: Optional[Path],
    profile,
    returncode: int,
    *,
    pre_launch_auth_dead: bool,
) -> None:
    """Record the launch's outcome. An exit code alone cannot tell EXHAUSTED
    from AUTH_DEAD from a plain crash -- the only case we have real evidence
    for is a profile the adapter already reported AuthDead on before we ever
    launched, so everything else unclassifiable maps to UNKNOWN rather than a
    guessed kind."""
    if quarantine is None:
        return
    if returncode == 0:
        quarantine.clear(profile.key)
    else:
        kind = AUTH_DEAD if pre_launch_auth_dead else UNKNOWN
        quarantine.record(profile.key, kind, detail=f"exit {returncode}")
    if quarantine_path is not None:
        save_quarantine(quarantine, quarantine_path)


def _cmd_run(
    rest: List[str],
    adapters,
    out,
    *,
    runner: Callable[..., Any],
    store=None,
    ttl_seconds: float = 3600.0,
    quarantine=None,
    quarantine_path: Optional[Path] = None,
) -> int:
    tokens = list(rest)
    dry_run = False
    if "--dry-run" in tokens:
        dry_run = True
        tokens = [t for t in tokens if t != "--dry-run"]
    if not tokens:
        print("eggswap run: missing profile-key", file=out)
        return 2
    profile_key, *tokens = tokens
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    child_args = tokens

    adapter, profile = _find_profile(adapters, profile_key)
    if profile is None:
        print(f"eggswap run: unknown profile {profile_key!r}", file=out)
        return 2

    pre_launch_auth_dead = False
    if quarantine is not None:
        try:
            pre_launch_auth_dead = isinstance(adapter.availability(profile), AuthDead)
        except Exception:  # pragma: no cover - a broken adapter must not block the launch
            pre_launch_auth_dead = False

    if profile.provider is Provider.CLAUDE:
        argv = adapter.launch_argv(profile, child_args)
        env: dict = {}
    else:
        env = adapter.launch_env(profile, dict(os.environ))
        argv = child_args

    if dry_run:
        print(json.dumps({"argv": argv, "env": {"CODEX_HOME": env["CODEX_HOME"]} if "CODEX_HOME" in env else {}}), file=out)
        return 0

    full_env = dict(os.environ)
    full_env.update(env)

    # The exclusive hold. Two Claude CLIs on one account's HOME concurrently
    # rotate a single-use refresh token and destroy it (whipstack #581) -- the
    # failure the Lease type exists to prevent. A README honesty audit found
    # that this CLI declared the guarantee and never took the lease, so the
    # type was doing nothing where it mattered. It does now.
    if store is None:
        result = runner(argv, env=full_env)
        returncode = getattr(result, "returncode", 0) or 0
        _settle_quarantine(
            quarantine, quarantine_path, profile, returncode,
            pre_launch_auth_dead=pre_launch_auth_dead,
        )
        return returncode

    try:
        lease = store.acquire(
            profile, ttl_seconds=ttl_seconds, holder=f"eggswap-cli:{os.getpid()}"
        )
    except LeaseError as exc:
        print(f"eggswap run: {profile.key} is held -- {exc}", file=out)
        return 10
    try:
        result = runner(argv, env=full_env)
        returncode = getattr(result, "returncode", 0) or 0
        _settle_quarantine(
            quarantine, quarantine_path, profile, returncode,
            pre_launch_auth_dead=pre_launch_auth_dead,
        )
        return returncode
    finally:
        try:
            store.release(lease)
        except StaleFence:
            # Our hold was reaped and re-granted while the child ran. Releasing
            # would evict the legitimate new holder, so refuse -- loudly, since
            # it means the child outlived its lease.
            print(
                f"eggswap run: lease on {profile.key} was superseded during the "
                "run; not releasing the replacement's hold",
                file=out,
            )


def _cmd_clear(profile_key: str, quarantine, quarantine_path: Optional[Path], out) -> int:
    """Release a quarantine by hand. AUTH_DEAD never expires on a timer (see
    core/quarantine.py), so a human who has re-logged in needs a way to say
    so; clearing an unknown key is a clean no-op, never an error."""
    if quarantine is not None:
        quarantine.clear(profile_key)
        if quarantine_path is not None:
            save_quarantine(quarantine, quarantine_path)
    print(f"cleared {profile_key}", file=out)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eggswap")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every profile, both providers, with honest availability")
    sub.add_parser("status", help="one-line summary + which profiles are schedulable")

    select_parser = sub.add_parser("select", help="print the chosen profile, do not launch")
    select_parser.add_argument("--provider", choices=[p.value for p in Provider], default=None)
    select_parser.add_argument("--json", action="store_true", dest="as_json")
    select_parser.add_argument(
        "--explain", action="store_true",
        help="print why this profile was chosen and why each other was refused",
    )
    select_parser.add_argument(
        "--cross-provider", dest="cross_provider", default="provider_order",
        choices=["provider_order", "most_absolute_headroom",
                 "longest_until_reset", "spread"],
        help="how to order ACROSS providers; the default is a policy choice, "
             "not a measurement, because a Claude 5h percentage and a Codex "
             "7d bucket are different units",
    )
    select_parser.add_argument(
        "--pin", default=None,
        help="demand one profile key; refuses rather than falling back if it is ineligible",
    )

    clear_parser = sub.add_parser("clear", help="release a hand-held quarantine")
    clear_parser.add_argument("profile_key")

    # "run" is parsed manually in main() because of the literal "--" separator
    # before the child command's own args; argparse's REMAINDER handling
    # would swallow --dry-run if it appeared after the profile key.
    sub.add_parser("run", help="print/exec the launch for a profile")

    return parser


def main(
    argv: List[str],
    *,
    adapters: Optional[Sequence[Any]] = None,
    out=sys.stdout,
    now: Optional[float] = None,
    runner: Callable[..., Any] = subprocess.run,
    store=None,
    quarantine=None,
    quarantine_path: Optional[Path] = None,
) -> int:
    resolved_adapters = _default_adapters() if adapters is None else list(adapters)
    resolved_now = time.time() if now is None else now
    # `store=False` disables the hold entirely (tests, and anyone who wants the
    # old behaviour); `store=None` means "use the real one on this machine".
    if store is None:
        from eggswap.core.store import LeaseStore

        resolved_store = LeaseStore(default_lease_root())
    else:
        resolved_store = store or None

    # `quarantine=<object>` (tests) is used in-memory as-is and never touches
    # disk; `quarantine=None` loads/persists the real on-disk failure memory,
    # the same EGGSWAP_STATE_DIR-relative file every `eggswap` invocation on
    # this machine shares. `quarantine=False` disables it entirely.
    if quarantine is None:
        resolved_quarantine_path = (
            Path(quarantine_path) if quarantine_path is not None else default_quarantine_path()
        )
        resolved_quarantine = load_quarantine(resolved_quarantine_path)
    elif quarantine is False:
        resolved_quarantine = None
        resolved_quarantine_path = None
    else:
        resolved_quarantine = quarantine
        resolved_quarantine_path = None

    if argv and argv[0] == "run":
        return _cmd_run(
            argv[1:], resolved_adapters, out, runner=runner, store=resolved_store,
            quarantine=resolved_quarantine, quarantine_path=resolved_quarantine_path,
        )

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list":
        policy = Policy()
        return _cmd_list(resolved_adapters, policy, out, now=resolved_now)
    if args.command == "status":
        policy = Policy()
        return _cmd_status(
            resolved_adapters, policy, out, now=resolved_now, store=resolved_store,
            quarantine=resolved_quarantine,
        )
    if args.command == "select":
        allow = (Provider(args.provider),) if args.provider else (Provider.CLAUDE, Provider.CODEX)
        policy = Policy(allow_providers=allow, pin=args.pin,
                        cross_provider=args.cross_provider)
        return _cmd_select(
            resolved_adapters, policy, out, now=resolved_now, as_json=args.as_json,
            store=resolved_store, quarantine=resolved_quarantine,
            explain=args.explain,
        )
    if args.command == "clear":
        return _cmd_clear(args.profile_key, resolved_quarantine, resolved_quarantine_path, out)

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


def main_entry() -> None:
    """Console-script entry point named by pyproject's [project.scripts].

    Separate from ``main`` because packaging calls it with NO arguments and
    expects it to exit the process, while ``main`` takes argv and returns an
    int so tests can drive it in-process. Exporting eggswap as a standalone
    repository is what caught this missing: pyproject promised
    ``eggswap.cli:main_entry`` and nothing of that name existed, which no
    in-tree test would ever have noticed.
    """
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main_entry()

"""Every core module must be reachable from something a user can run.

Four times in one session this project shipped a working thing that nothing
could reach: a fenced lease `eggswap run` never acquired, a quarantine with
zero callers, a `--explain` flag that parsed and did nothing, and a policy
option no command line exposed. Each had passing tests, because a unit test
imports the module directly and therefore cannot notice that production does
not.

This is that mistake turned into a check. It greps for imports rather than
building a call graph, but it does follow them TRANSITIVELY: the first version
looked only at the entry-point files and reported `core/policy.py` unreachable
when it is reached perfectly well through `core/select.py`. A check that cries
wolf gets an allowlist entry added to silence it, and then it protects
nothing -- so the fix was the check, not the allowlist.

UNWIRED is an explicit allowlist, not a silent pass. A module may legitimately
have no caller yet; what may not happen is that fact going unnoticed. Adding a
name here is a visible act in a diff, and every entry must say why.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "eggswap" / "core"
ADAPTERS = ROOT / "eggswap" / "adapters"

#: module name -> why it has no production caller yet.
UNWIRED = {
    "budget": (
        "needs a real spending source to mean anything; wiring it to a "
        "fabricated cost would be worse than leaving it visible here"
    ),
}

#: Things a user can actually invoke.
ENTRY_POINTS = [
    ROOT / "eggswap" / "cli.py",
    ROOT / "bin" / "eggswap-acceptance",
    ROOT / "bin" / "eggswap-handoff-demo",
]


def _modules(directory: Path) -> list[str]:
    return sorted(
        p.stem for p in directory.glob("*.py")
        if p.stem != "__init__" and not p.stem.startswith("_")
    )


def _reachable_modules() -> set[str]:
    """Modules reachable from an entry point, following imports transitively.

    Fixpoint rather than one hop: cli.py imports core.select, and core.select
    imports core.policy, so policy IS reachable even though no entry point
    names it.
    """
    by_name = {}
    for directory in (CORE, ADAPTERS):
        for path in directory.glob("*.py"):
            if path.stem != "__init__" and not path.stem.startswith("_"):
                by_name[path.stem] = path.read_text()

    frontier = "\n".join(p.read_text() for p in ENTRY_POINTS if p.is_file())
    reached: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, source in by_name.items():
            if name in reached:
                continue
            needles = (f"core.{name}", f"core import {name}",
                       f"adapters.{name}", f"adapters import {name}")
            if any(n in frontier for n in needles):
                reached.add(name)
                frontier += "\n" + source
                changed = True
    return reached


class CoreModulesAreReachableTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(any(p.is_file() for p in ENTRY_POINTS),
                        "no entry point was readable; this check would pass vacuously")
        self.reached = _reachable_modules()
        self.assertTrue(self.reached, "nothing was reachable at all; the walk is broken")

    def _reachable(self, module: str) -> bool:
        return module in self.reached

    def test_every_core_module_is_reachable_or_listed(self):
        for module in _modules(CORE):
            with self.subTest(module=module):
                if module in UNWIRED:
                    continue
                self.assertTrue(
                    self._reachable(module),
                    f"eggswap/core/{module}.py is reachable from no entry point. "
                    f"Wire it, or add it to UNWIRED with the reason.",
                )

    def test_every_adapter_is_reachable(self):
        for module in _modules(ADAPTERS):
            with self.subTest(module=module):
                self.assertTrue(
                    self._reachable(module),
                    f"eggswap/adapters/{module}.py is reachable from no entry point.",
                )

    def test_the_allowlist_has_no_stale_entries(self):
        """A module that got wired must LEAVE the list.

        Otherwise the allowlist becomes a place names go to be forgotten,
        which is the same failure wearing a tidier coat. This caught its own
        first draft: `handoff` sat here while being perfectly reachable from
        bin/eggswap-handoff-demo, so the list was already lying one commit
        after it was written.
        """
        existing = set(_modules(CORE)) | set(_modules(ADAPTERS))
        for module in UNWIRED:
            with self.subTest(module=module):
                self.assertIn(module, existing,
                              f"UNWIRED names {module!r}, which no longer exists")
                self.assertFalse(
                    self._reachable(module),
                    f"UNWIRED names {module!r} but it IS reachable now -- "
                    "remove it from the list rather than leaving a stale excuse",
                )

    def test_every_allowlist_entry_states_a_reason(self):
        for module, reason in UNWIRED.items():
            with self.subTest(module=module):
                self.assertGreater(len(reason.strip()), 20,
                                   f"UNWIRED[{module!r}] must say WHY, not just exist")

    def test_the_check_can_fail(self):
        """Negative control: a made-up module must be reported unreachable."""
        self.assertFalse(self._reachable("no_such_module_exists_anywhere"))

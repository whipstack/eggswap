"""Packaging artifacts are checked like code, because they are read like code.

WHY THIS FILE EXISTS
---------------------
LICENSE, NOTICE, pyproject.toml and CONTRIBUTING.md are prose/config, not
Python, so nothing else in the test suite would ever notice if one of them
regressed -- e.g. a future edit that quietly adds a runtime dependency to
pyproject.toml, or that drops the MIT copyright line from LICENSE. Each
assertion here is chosen so that it can actually FAIL against a plausible
mistake, not just echo the file back at itself.
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: tomllib landed in 3.11.
    tomllib = None

def _project_dir() -> Path:
    """Where eggswap's project metadata lives, in EITHER layout.

    In the development checkout the package directory also holds the packaging files
    (``<repo>/eggswap/pyproject.toml``); once exported as a standalone
    repository they sit at the root beside the package. Hardcoding the first
    layout made every test here pass in-tree and ERROR in the artifact we
    actually ship -- found by running this suite against the export.
    """
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / "eggswap", here):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError(f"no pyproject.toml under {here}")


EGGSWAP_DIR = _project_dir()


class TestPyprojectToml(unittest.TestCase):
    def _load(self):
        if tomllib is None:
            self.skipTest("tomllib is stdlib only from Python 3.11 onward")
        path = EGGSWAP_DIR / "pyproject.toml"
        with path.open("rb") as fh:
            return tomllib.load(fh)

    def test_parses_as_toml(self) -> None:
        data = self._load()
        self.assertIn("project", data)

    def test_declares_name_eggswap(self) -> None:
        data = self._load()
        self.assertEqual(data["project"]["name"], "eggswap")

    def test_declares_mit_license(self) -> None:
        data = self._load()
        license_field = data["project"]["license"]
        if isinstance(license_field, dict):
            license_text = license_field.get("text", "")
        else:
            license_text = str(license_field)
        self.assertIn("MIT", license_text)

    def test_requires_python_310_or_newer(self) -> None:
        data = self._load()
        self.assertEqual(data["project"]["requires-python"], ">=3.10")

    def test_dependencies_list_is_empty(self) -> None:
        """The stdlib-only rule, made enforceable.

        This is the important assertion in this file: it turns "eggswap has
        zero runtime dependencies" from a claim in prose into a build that
        breaks here, in this repository, the moment a contributor adds
        ``requests`` or anything else to ``dependencies`` -- rather than
        breaking silently in whatever downstream environment installs it.
        """
        data = self._load()
        self.assertEqual(data["project"].get("dependencies", None), [])

    def test_declares_console_script_entry_point(self) -> None:
        data = self._load()
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts.get("eggswap"), "eggswap.cli:main_entry")

    def test_platform_classifiers_match_the_posix_lease_store(self) -> None:
        data = self._load()
        classifiers = data["project"]["classifiers"]
        self.assertNotIn("Operating System :: OS Independent", classifiers)
        self.assertIn("Operating System :: POSIX", classifiers)
        self.assertIn("Operating System :: POSIX :: Linux", classifiers)
        self.assertIn("Operating System :: MacOS :: MacOS X", classifiers)


class TestStandaloneCiAndEvidence(unittest.TestCase):
    def test_ci_workflow_is_exportable_and_runs_tests_and_build(self) -> None:
        workflow = EGGSWAP_DIR / ".github" / "workflows" / "ci.yml"
        self.assertTrue(workflow.is_file(), f"missing standalone CI: {workflow}")
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover", text)
        self.assertIn("python -m build --wheel", text)
        self.assertIn("eggswap --help", text)
        self.assertIn("contents: read", text)

    def test_portable_codex_evidence_is_present_without_internal_notes(self) -> None:
        project_root = EGGSWAP_DIR.parent if EGGSWAP_DIR.name == "eggswap" else EGGSWAP_DIR
        evidence_dir = project_root / "docs/research/eggswap"
        self.assertTrue((evidence_dir / "codex-account-model.md").is_file())
        self.assertTrue((evidence_dir / "codex-ratelimits-live.md").is_file())
        readme = (EGGSWAP_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("codex-ratelimits-live.md", readme)
        if EGGSWAP_DIR.name != "eggswap":
            self.assertEqual(
                {path.name for path in evidence_dir.glob("*.md")},
                {"codex-account-model.md", "codex-ratelimits-live.md"},
            )


class TestLicenseFile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (EGGSWAP_DIR / "LICENSE").read_text(encoding="utf-8")

    def test_contains_mit_permission_grant(self) -> None:
        self.assertIn(
            "Permission is hereby granted, free of charge, to any person "
            "obtaining a copy",
            self.text,
        )

    def test_contains_2026_copyright_line(self) -> None:
        self.assertIn("Copyright (c) 2026 Vadim Surin", self.text)


class TestNoticeFile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (EGGSWAP_DIR / "NOTICE").read_text(encoding="utf-8")

    def test_names_upstream_project(self) -> None:
        self.assertIn("realiti4/claude-swap", self.text)

    def test_states_upstream_is_mit(self) -> None:
        self.assertIn("MIT", self.text)

    def test_states_no_vendored_code(self) -> None:
        lowered = self.text.lower()
        self.assertIn("does not vendor", lowered)


class TestContributingFile(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (EGGSWAP_DIR / "CONTRIBUTING.md").read_text(encoding="utf-8")

    def test_states_stdlib_only_rule(self) -> None:
        lowered = self.text.lower()
        self.assertIn("stdlib", lowered)
        self.assertIn("dependency", lowered)

    def test_states_unreadable_account_review_rule(self) -> None:
        lowered = self.text.lower()
        self.assertIn("unreadable account", lowered)
        self.assertIn("rejected", lowered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

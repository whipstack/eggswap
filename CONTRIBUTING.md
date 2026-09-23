# Contributing to eggswap

## Running the tests

```
python3 -m unittest discover -s tests
```

Tests use the stdlib `unittest` module only. Do not add a dependency on
`pytest` (or anything else) to run or write tests — assume it is not
installed.

## The stdlib-only rule

eggswap ships **zero runtime dependencies**. `eggswap/pyproject.toml`
declares an empty `dependencies` list, and a test asserts it stays empty.
Every module starts with `from __future__ import annotations` and targets
Python >= 3.10. If your change needs a package from PyPI, it does not belong
in this repository's runtime path — discuss it in an issue first.

## The one review rule that matters here

**A change that can make an unreadable account look available will be
rejected.** That covers anything that turns a failed read, a timeout, a
missing field, or a stale cache entry into `Available`, into `0%` used, or
into a number that renders without its age. See `eggswap/core/types.py` for
why this rule exists and what it cost the estate before it was encoded as a
type.

Everything else — style, naming, test coverage for the easy cases — is a
normal code review conversation. That one is not negotiable.

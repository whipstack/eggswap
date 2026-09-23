"""Provider adapters.

Deliberately empty of re-exports. Each adapter is imported by its own module
path (``eggswap.adapters.claude_cswap``, ``eggswap.adapters.codex_home``) so
that a provider whose tooling is absent on a machine cannot break the import
of the one that is present -- importing a package must never require every
vendor's CLI to exist.
"""

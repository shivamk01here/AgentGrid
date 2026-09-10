# Working prompt

Paste this at the start of a session when you want another slice of work on
this repo.

---

Pick up Ledgerloop and move it forward. Either build one new feature or fix a
handful of real bugs - or, better, do some of both in the same stretch of
work.

**Shape of the work**

- Land it in **8 to 11 commits**. Pick a number in that range that suits the
  work; do not pad it out to hit a target and do not squash it down to two.
- Mix the commits up. Some carry the feature, some fix bugs you actually found
  while reading the code. A commit that only fixes a typo is not a bug fix.
- One idea per commit. Source, then its tests, then the wiring, then the docs -
  in that order, so every commit stands on its own.
- Fix real bugs. Read the code first and find something that is genuinely
  wrong: a missing import, a count that lies, a branch that can't be reached,
  a guarantee the docstring claims and the code doesn't keep. Don't invent a
  bug so you have one to fix.

**Commit messages**

Write them the way a person writes them at the end of an afternoon. Lowercase
subject, no trailing full stop, no `feat:` / `fix:` / `chore:` prefixes, no
ticket numbers, no bullet lists. A short subject line, a blank line, then a
paragraph or two of plain English explaining *why* - what would have gone
wrong without this, what you got wrong the first time, what you deliberately
left out. Look at `git log` and match that voice. Examples of the register:

    reconciler for stuck in-flight claims
    tests for the executor
    export the runtime bits, add a runnable example, update readme

**Credentials**

Commit as the same author as every other commit in this repo. That identity is
already in the repo's git config - use it as-is. Do not pass `--author`, do
not set `user.name` or `user.email`, and do not invent a new identity.

**House style**

- Read the neighbouring module before writing a new one and match it: module
  docstring that explains the *why*, `from __future__ import annotations`,
  `__all__`, `TYPE_CHECKING` imports for typing-only names, frozen slotted
  dataclasses, keyword-only constructor arguments, no imports inside function
  bodies.
- Money is integer minor units plus a currency, always.
- Anything that moves value is claimed against the idempotency store before it
  is dispatched, and an unknown outcome is reconciled, never retried.
- Ports are async protocols in `core/ports.py`. The runtime depends on those,
  never on an adapter.
- Tests go under `tests/<package>/`, grouped in `Test...` classes, named after
  the thing that would go wrong.

**Before you finish**

Run `ruff check src/ tests/`, `mypy src/ledgerloop/` and `pytest` if the tools
are installed. If they aren't, say so plainly rather than claiming the suite
is green.

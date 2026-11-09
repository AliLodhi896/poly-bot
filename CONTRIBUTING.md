# Contributing

Issues and pull requests are welcome. This is a trading bot, so a few things are non-negotiable.

## Ground rules

- **No geoblock bypass.** Polybot checks Polymarket's official geoblock endpoint and refuses to
  trade where restricted. PRs that circumvent, spoof or weaken that check will be closed without
  discussion. The same goes for browser impersonation to defeat bot challenges.
- **Paper stays the default.** Any change that makes real orders easier to place by accident —
  removing a gate, defaulting a live flag to true, widening what counts as a fresh dry-run — needs
  a very good reason and a test.
- **Never commit credentials.** `.env`, private keys, API keys, and the sqlite databases are
  gitignored. Check `git diff --cached` before committing.

## Before opening a PR

```bash
uv run pytest
uv run ruff check .
cd web && npx tsc --noEmit && npx eslint src
```

Changes to strategy behaviour should come with a test in `tests/`, and — where the change is
about trade quality rather than correctness — a note on what you measured. "It felt better" is not
a result; fill-before-cancel rate, bundle completion rate and realized reward vs estimate are.

## Scope

Things that are in scope: bug fixes, new reward-market filters and guards, better fill modelling,
execution robustness, docs, tests.

Things that are out of scope: turning this into a hosted service, custodial wallet management,
anything that takes other people's funds. That was tried, and deliberately removed.

## Reporting a security issue

If you find something that could cost someone money — a way to leak a key, place an unintended
order, or bypass a gate — please report it privately. See [SECURITY.md](SECURITY.md).

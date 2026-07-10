# Snowball Package Guide

`snowball` is the AutoForexV2 strategy package for Snowball automated trading
logic.

## Responsibilities

- Provide Snowball strategy models, rules, signals, and strategy-specific task
  building blocks.
- Use `core` for shared domain models, calculations, and trading primitives.
- Keep the strategy reusable by `server`.

## Boundaries

- Do not perform direct OANDA communication here; use `oanda` through
  orchestration in `server`.
- Do not expose HTTP or gRPC endpoints here.
- Do not put generic AutoForex domain behavior here; use `core`.

## Commit Policy

- Use Conventional Commits for all commits: `<type>(<scope>): <summary>`.
- Prefer the package name as the scope for package-local changes, for example
  `docs(snowball): require conventional commits`.
- Use one of `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, or `revert`.
- Keep summaries imperative, concise, and without a trailing period.
- For breaking changes, append `!` after the type/scope and include a
  `BREAKING CHANGE:` footer when more detail is needed.

## Commands

```bash
uv sync
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pytest
```

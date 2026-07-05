# snowball

Snowball trading strategy package for AutoForexV2.

## Architecture

Snowball keeps the trading strategy in domain objects and services. The main
boundaries are:

- `models`: grid, entry, identifier, and state objects. These own lifecycle
  invariants and use Core value objects, but do not depend on Core strategy
  adapter types such as `StrategyState` or `StrategyEvent`.
- `events`: Snowball domain events only.
- `event_mapper` and `serialization`: adapter boundaries for Core strategy
  events and Core strategy state.
- `services/market_pricing.py`: executable market-price and P/L math. It has no
  Snowball config or policy dependency.
- `services/policies`: configurable Snowball policy decisions such as position
  sizing, take-profit planning, stop-loss planning, and grid ordering.
- `services/selectors`: read-only grid selectors used by flows.
- `services/flows`: use-case services that mutate grid state and emit domain
  events.
- `services/stages`: tick and cycle processing stages.
- `composition.py`: the service graph assembly point for the engine.
- `engine.py`: tick orchestration only.

The architecture tests in `tests/test_architecture.py` protect these
boundaries, including the current service module layout.

## Setup

```bash
uv sync
```

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run ty check
uv run pytest
```

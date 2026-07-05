import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "snowball"

ADAPTER_SYMBOLS = {
    "StrategyAction",
    "StrategyContext",
    "StrategyDecisionCode",
    "StrategyDecisionReason",
    "StrategyEvent",
    "StrategyState",
    "TaskType",
    "TradeSide",
}

OLD_SERVICE_REFERENCES = {
    "snowball.services.close_service",
    "snowball.services.counter_service",
    "snowball.services.cycle_service",
    "snowball.services.entry_service",
    "snowball.services.event_factory",
    "snowball.services.grid_policy",
    "snowball.services.grid_selectors",
    "snowball.services.position_sizing",
    "snowball.services.pricing",
    "snowball.services.protection_service",
    "snowball.services.rebuild_service",
    "snowball.services.stop_loss_close_service",
    "snowball.services.stop_loss_policy",
    "snowball.services.take_profit_close_service",
    "snowball.services.take_profit_policy",
    "snowball.services.tick_stages",
    "SnowballCloseService",
    "SnowballPricing",
}


def imported_core_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "core":
            symbols.update(alias.name for alias in node.names)
    return symbols


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def python_paths() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_domain_models_and_events_do_not_import_core_adapter_types() -> None:
    domain_paths = [*sorted((PACKAGE / "models").glob("*.py")), PACKAGE / "events.py"]

    violations = {
        path.relative_to(ROOT).as_posix(): sorted(imported_core_symbols(path) & ADAPTER_SYMBOLS)
        for path in domain_paths
        if imported_core_symbols(path) & ADAPTER_SYMBOLS
    }

    assert violations == {}


def test_market_pricing_has_no_config_or_policy_dependencies() -> None:
    modules = imported_modules(PACKAGE / "services" / "market_pricing.py")

    assert "snowball.config" not in modules
    assert "snowball.enums" not in modules
    assert not any(module.startswith("snowball.services.policies") for module in modules)


def test_old_service_paths_are_not_used() -> None:
    violations: dict[str, list[str]] = {}
    for path in python_paths():
        text = path.read_text()
        matches = sorted(reference for reference in OLD_SERVICE_REFERENCES if reference in text)
        if matches:
            violations[path.relative_to(ROOT).as_posix()] = matches

    assert violations == {}

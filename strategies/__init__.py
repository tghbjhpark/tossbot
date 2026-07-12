from strategies.base import BaseStrategy
from strategies.grid import GridStrategy

STRATEGY_MAP = {
    "GRID": GridStrategy,
}

def get_strategy_class(strategy_name: str):
    """
    Returns the strategy class for a given strategy name.
    Defaults to GridStrategy if not found.
    """
    name = strategy_name.upper().strip()
    return STRATEGY_MAP.get(name, GridStrategy)

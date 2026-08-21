from strategies.base import BaseStrategy
from strategies.grid import GridStrategy
from strategies.dca import DcaStrategy
from strategies.vr import VrStrategy
from strategies.crsi import CrsiStrategy

STRATEGY_MAP = {
    "GRID": GridStrategy,
    "DCA": DcaStrategy,
    "VR": VrStrategy,
    "CRSI": CrsiStrategy,
}

def get_strategy_class(strategy_name: str):
    """
    Returns the strategy class for a given strategy name.
    Defaults to GridStrategy if not found.
    """
    name = strategy_name.upper().strip()
    return STRATEGY_MAP.get(name, GridStrategy)

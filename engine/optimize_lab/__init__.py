"""optimize-lab reference engine.

Discrete-event simulation of one service location's operating day, run over
paired Monte-Carlo days, comparing baseline FIFO operation against a set of
optimization levers. Input contract: schemas/scenario-config.v1.json.
Output contract: schemas/results-report.v1.json. Both are LOCKED; this
package builds to them.
"""

__version__ = "0.1.0"

from .config import load_scenario  # noqa: F401

"""Interactive control and monitoring for training loops.

The TUI lives in trainctl.tui and is never imported from here, so headless
installs don't need its dependencies.
"""

from .controller import Controller, NullController
from .run import run

__all__ = ["Controller", "NullController", "run"]

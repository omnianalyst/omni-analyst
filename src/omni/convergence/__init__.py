"""Convergence: the symmetric counterpart to the contradiction detector.

`coverage/gaps.py` surfaces independent sources disagreeing. This package
surfaces independent claim families agreeing about one entity inside a window,
counted by family so that one flooding source can never look like corroboration.
"""

from omni.convergence.detect import (
    CLAIM_FAMILIES,
    Convergence,
    detect,
    detect_all,
)

__all__ = ["CLAIM_FAMILIES", "Convergence", "detect", "detect_all"]

"""Editorial dials held as bitemporal claims rather than code constants.

A dial changed in code rewrites the past; a dial changed here is a new claim
with its own knowledge_date, so a point-in-time read still sees the value that
was in force. Dials are features and priors only -- never thresholds, and never
read by the conviction gate. See `store` for the full rule.
"""

from omni.dials.store import Dial, get_dial, history, set_dial

__all__ = ["Dial", "get_dial", "history", "set_dial"]

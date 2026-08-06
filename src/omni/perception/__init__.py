"""
Behavioural-finance perception engine: sentiment/price dynamics.

herding.py and fomo.py were removed -- both were orphan (no capability
registration) and carried the float-zero / fillna-as-signal fabrication defects
the project refuses. The one live behavioural concept, perception-vs-fundamentals
divergence, lives in divergence.py (and reads this module's _analyze_sentiment_divergence).
"""
__all__ = ["SentimentDynamicsModel"]

try:
    from .dynamics import SentimentDynamicsModel
except ImportError:
    pass

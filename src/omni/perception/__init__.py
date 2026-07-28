"""
Behavioural-finance perception engines: sentiment/price dynamics, herding, FOMO.

Pure functions on DataFrames; no IO, no framework coupling.
"""
__all__ = ['FOMODetector', 'HerdingAnalyzer', 'SentimentDynamicsModel']

try:
    from .dynamics import SentimentDynamicsModel
except ImportError:
    pass

try:
    from .fomo import FOMODetector
except ImportError:
    pass

try:
    from .herding import HerdingAnalyzer
except ImportError:
    pass

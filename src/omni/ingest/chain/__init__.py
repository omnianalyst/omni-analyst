"""Chain cursor: resumable, monotonic per-chain block indexing.

Re-exports the public surface; no logic lives here. See ``cursor`` for the
traversal design and the monotonicity guarantee.
"""

from omni.ingest.chain.cursor import BlockRange, advance, get_cursor, next_range

__all__ = ["BlockRange", "advance", "get_cursor", "next_range"]

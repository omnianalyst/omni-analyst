"""Publish the local hypothesis registry to a database so the UI can show it.

WHY THIS IS A SCRIPT AND NOT A LOOP:
    The registry is a JSONL file at `_orchestrator/hypothesis_registry.jsonl`,
    and the Docker image ships only `src/` and `migrations/`. A deployed API
    therefore has no copy of the file and never will -- research runs happen
    wherever the researcher is, not in the container. So the sync has to be
    pushed from the machine holding the file, after a research session, rather
    than pulled by a scheduler that cannot see it.

WHAT IT DOES NOT DO:
    It does not write to the registry, compute a bar, or judge anything. The
    JSONL file remains the single writer; this copies it. Two writers would give
    two different N for `sqrt(2 ln N)`, and the disagreement would be invisible
    because both numbers would look plausible.

    It is also one-way. Rows already in the table are left alone: the mirror is
    idempotent on (name, recorded_at), so re-running it is a no-op rather than a
    duplicate, and a row whose registry entry was somehow lost is not deleted.
    An append-only record that a sync can silently shrink is not append-only.

Run:
    python ops/publish_research.py
    python ops/publish_research.py --registry /path/to/hypothesis_registry.jsonl
    python ops/publish_research.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("ops.publish_research")


async def main(argv: Sequence[str] | None = None) -> int:
    from omni.config import settings
    from omni.db import connect
    from omni.research.publish import mirror_registry, read_history, summarise
    from omni.research.registry import Registry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Mirror the research registry into the hypothesis_test table.",
    )
    parser.add_argument(
        "--registry",
        default=None,
        help="Path to hypothesis_registry.jsonl (defaults to the repo copy)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without writing it",
    )
    args = parser.parse_args(argv)

    registry = Registry(path=Path(args.registry) if args.registry else None)
    entries = registry.entries()

    if not entries:
        # An empty registry is not an error, but it is almost always a wrong
        # --registry path rather than a project that has tested nothing. Say
        # which file was read so the operator can tell the two apart.
        logger.warning("no entries found at %s", registry.path)
        return 1

    logger.info(
        "read %d entries (%d statistics) from %s",
        len(entries),
        sum(e.cells for e in entries),
        registry.path,
    )

    if args.dry_run:
        for entry in entries[-5:]:
            logger.info("  %s  %s  cells=%d", entry.recorded_at, entry.name, entry.cells)
        logger.info("dry run: nothing written")
        return 0

    client = await connect(settings.database_url)
    try:
        report = await mirror_registry(client.pool, registry=registry)
        summary = summarise(await read_history(client.pool))
    finally:
        await client.close()

    logger.info(
        "mirrored: %d new, %d already present", report.inserted, report.already_present
    )
    logger.info(
        "record now holds %d tests / %d statistics; bar %.3f, FDR bar %.3f",
        summary["tests"],
        summary["cells"],
        summary["bar"],
        summary["fdr_bar"],
    )
    if summary["passed"] == 0:
        logger.info("nothing has cleared the bar")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

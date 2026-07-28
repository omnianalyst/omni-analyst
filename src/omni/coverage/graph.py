"""Cross-domain traversal over the entity graph.

`entity_edge` is what stops this being four stores glued together. BTC relates
to COIN, crude to XLE, the Fed funds rate to everything. Traversal turns those
links into the one thing a coverage network can do that four dashboards cannot:
explain why an unrelated-looking entity is relevant, and surface the coverage
its neighbourhood holds.

Every claim read in this module goes through `omni.coverage.visibility`. A
traversal that reads around it serves one user's BYO-licensed data to another,
which is the redistribution leak the schema exists to prevent. `claim` is never
queried directly: the visibility fragment is composed into the traversal as a
CTE, so the audience rule is applied before any hop tagging.

Traversal is done in SQL with a recursive CTE, not by looping queries in Python.
N+1 round trips over a graph is how this becomes the slowest part of the
system, and a single CTE lets Postgres plan the whole walk at once.
"""

from __future__ import annotations

from uuid import UUID

from omni.coverage.visibility import visible_claims_cte

# `related_coverage` depth is capped here. On a dense graph (an index ETF and
# its holdings, a supplier graph, a cross-holding web) the reachable set grows
# roughly with the branching factor raised to the depth, so depth 3 on a
# well-connected entity reaches most of the store. Beyond that the result
# stops being "this entity's neighbourhood" and becomes "the whole network,
# weakly related", which is both expensive to compute and uninformative to
# surface. The cap is also a correctness backstop: even with cycle protection
# in the CTE, an uncapped walk on a cyclic dense graph is unbounded work for
# a question nobody asked. Two hops covers subject -> direct neighbour ->
# neighbour-of-neighbour, which is the useful range for cross-domain analysis.
MAX_DEPTH = 2


async def relate(
    pool,
    from_entity: UUID,
    to_entity: UUID,
    *,
    relation: str,
    weight: float = 1.0,
    source: str,
) -> None:
    """Record a directed edge, idempotent on (from_entity, to_entity, relation).

    A repeat updates weight rather than raising: the primary key is the edge's
    identity, and a re-assertion of the same relationship with a revised weight
    is an update to that edge, not a new one. The schema's self-loop CHECK
    rejects from_entity == to_entity at the database boundary.
    """
    await pool.execute(
        """
        INSERT INTO entity_edge (from_entity, to_entity, relation, weight, source)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (from_entity, to_entity, relation)
        DO UPDATE SET weight = EXCLUDED.weight
        """,
        from_entity,
        to_entity,
        relation,
        weight,
        source,
    )


async def neighbours(
    pool,
    entity_id: UUID,
    *,
    relation: str | None = None,
    min_weight: float | None = None,
) -> list[dict]:
    """Direct edges of `entity_id`, queryable in either direction.

    Edges are directed (A -> B is distinct from B -> A) but the reverse index
    on `entity_edge` exists so a caller asking "what points at A" pays the same
    as one asking "what does A point at". Each result carries the direction so
    a caller can tell "BTC influences COIN" (out) from the converse (in).

    Returns one row per incident edge, not per neighbour entity: a true cycle
    (A -> B and B -> A, same relation) yields two rows for B, one each way,
    which is the truthful picture of the graph.
    """
    conditions = []
    params: list = [entity_id]
    if relation is not None:
        params.append(relation)
        rel_cond = f"e.relation = ${len(params)}"
        conditions.append(rel_cond)
    if min_weight is not None:
        params.append(min_weight)
        conditions.append(f"e.weight >= ${len(params)}")

    rel_clause = ""
    if conditions:
        rel_clause = " AND " + " AND ".join(conditions)

    sql = f"""
        SELECT e.to_entity AS entity_id, e.relation, e.weight, e.source,
               'out'::text AS direction
        FROM entity_edge e
        WHERE e.from_entity = $1{rel_clause}
        UNION ALL
        SELECT e.from_entity AS entity_id, e.relation, e.weight, e.source,
               'in'::text AS direction
        FROM entity_edge e
        WHERE e.to_entity = $1{rel_clause}
        ORDER BY direction, relation
    """
    rows = await pool.fetch(sql, *params)
    return [dict(r) for r in rows]


async def related_coverage(
    pool,
    entity_id: UUID,
    *,
    audience: UUID | None,
    depth: int = 1,
    relation: str | None = None,
) -> list[dict]:
    """Visible claims held about `entity_id` and its graph neighbours.

    The subject's own claims carry hop 0; each neighbour's claims carry the
    shortest hop distance to it. Claims are read through the visibility CTE,
    so a neighbour's `byo_only` claim owned by another user does not appear
    here -- composing the audience rule into the CTE is what stops a traversal
    from becoming a redistribution leak.

    `depth` is capped at MAX_DEPTH. `relation`, when given, scopes which edges
    are traversed (not the subject's own claims, which are always included).
    """
    capped = min(depth, MAX_DEPTH)

    sql = f"""
        WITH RECURSIVE reach(entity_id, hop, path) AS (
            SELECT id, 0, ARRAY[id] FROM entity WHERE id = $1
            UNION ALL
            SELECT edge.other, r.hop + 1, r.path || edge.other
            FROM reach r
            CROSS JOIN LATERAL (
                SELECT to_entity AS other, relation FROM entity_edge
                WHERE from_entity = r.entity_id
                  AND ($3::text IS NULL OR relation = $3)
                UNION ALL
                SELECT from_entity AS other, relation FROM entity_edge
                WHERE to_entity = r.entity_id
                  AND ($3::text IS NULL OR relation = $3)
            ) edge
            WHERE r.hop < $2
              AND edge.other <> ALL(r.path)
        ),
        nearest AS (
            SELECT entity_id, MIN(hop) AS hop FROM reach GROUP BY entity_id
        ),
        vis AS (
            {visible_claims_cte("$4")}
        )
        SELECT vis.id, vis.entity_id, vis.claim_type, vis.key, vis.value,
               vis.unit, vis.evidence, vis.source, vis.event_date,
               vis.knowledge_date, vis.confidence, vis.credential_owner,
               vis.redistributable, nearest.hop
        FROM nearest
        JOIN vis ON vis.entity_id = nearest.entity_id
        ORDER BY nearest.hop, vis.knowledge_date DESC, vis.event_date DESC
    """
    rows = await pool.fetch(sql, entity_id, capped, relation, audience)
    return [dict(r) for r in rows]


async def find_path(
    pool,
    from_entity: UUID,
    to_entity: UUID,
    *,
    max_depth: int = 3,
) -> dict | None:
    """Shortest relation path between two entities, or None.

    Breadth-first via a recursive CTE: the first row to reach `to_entity` at
    the smallest depth is the shortest path, because each recursion level is
    exactly one hop further than the last. Cycle protection is per-path
    (`other <> ALL(path)`), so the walk terminates on cyclic graphs without
    needing the depth bound -- the bound is a cost control, not a correctness
    one.

    Returns ``{"entities": [...], "relations": [...]}`` where `relations` has
    one entry per hop (the edge label traversed to reach the next entity) and
    `entities` has one more entry than `relations`.
    """
    sql = """
        WITH RECURSIVE search(entity_id, path, relations, depth) AS (
            SELECT $1::uuid, ARRAY[$1]::uuid[], ARRAY[]::text[], 0
            UNION ALL
            SELECT edge.other,
                   s.path || edge.other,
                   s.relations || edge.relation,
                   s.depth + 1
            FROM search s
            CROSS JOIN LATERAL (
                SELECT to_entity AS other, relation FROM entity_edge
                WHERE from_entity = s.entity_id
                UNION ALL
                SELECT from_entity AS other, relation FROM entity_edge
                WHERE to_entity = s.entity_id
            ) edge
            WHERE s.depth < $3
              AND edge.other <> ALL(s.path)
        )
        SELECT path AS entities, relations, depth
        FROM search
        WHERE entity_id = $2
        ORDER BY depth
        LIMIT 1
    """
    row = await pool.fetchrow(sql, from_entity, to_entity, max_depth)
    if row is None:
        return None
    return {
        "entities": [str(e) for e in row["entities"]],
        "relations": list(row["relations"]),
        "depth": row["depth"],
    }

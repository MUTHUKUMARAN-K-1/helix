"""Planner quality scorer: Node F1, Edge F1, partial-order accuracy, exact match.

These are the standard orchestration-quality metrics, implemented openly so
every published number can be rerun. Score a planner's DAG against a reference
decomposition for the same goal.

Node matching is by normalized label similarity (exact id match not required):
a predicted node counts as matched when its task text best matches a reference
node above the threshold, one-to-one greedy by score.
"""
from __future__ import annotations

import difflib
from typing import Any

from .schema import PlanSpec


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def match_nodes(pred: PlanSpec, ref: PlanSpec, threshold: float = 0.55) -> dict[str, str]:
    """Greedy one-to-one matching: pred node id -> ref node id."""
    scores = []
    for p in pred.nodes:
        for r in ref.nodes:
            s = max(_sim(p.task, r.task), _sim(p.id.replace("_", " "), r.id.replace("_", " ")))
            scores.append((s, p.id, r.id))
    scores.sort(reverse=True)
    matched_p, matched_r, out = set(), set(), {}
    for s, pid, rid in scores:
        if s < threshold:
            break
        if pid not in matched_p and rid not in matched_r:
            matched_p.add(pid)
            matched_r.add(rid)
            out[pid] = rid
    return out


def _edges(plan: PlanSpec) -> set[tuple[str, str]]:
    return {(d, n.id) for n in plan.nodes for d in n.depends_on}


def _f1(tp: int, pred_n: int, ref_n: int) -> float:
    if pred_n == 0 or ref_n == 0:
        return 0.0
    p, r = tp / pred_n, tp / ref_n
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def score_plan(pred: PlanSpec, ref: PlanSpec) -> dict[str, Any]:
    m = match_nodes(pred, ref)

    node_f1 = _f1(len(m), len(pred.nodes), len(ref.nodes))

    pred_edges = {(a, b) for (a, b) in _edges(pred) if a in m and b in m}
    mapped_pred_edges = {(m[a], m[b]) for (a, b) in pred_edges}
    ref_edges = _edges(ref)
    edge_tp = len(mapped_pred_edges & ref_edges)
    edge_f1 = _f1(edge_tp, len(mapped_pred_edges), len(ref_edges))

    def before(plan: PlanSpec) -> set[tuple[str, str]]:
        reach: set[tuple[str, str]] = set()
        children: dict[str, list[str]] = {}
        for n in plan.nodes:
            for d in n.depends_on:
                children.setdefault(d, []).append(n.id)
        for n in plan.nodes:
            stack = [n.id]
            while stack:
                cur = stack.pop()
                for nxt in children.get(cur, []):
                    if (n.id, nxt) not in reach:
                        reach.add((n.id, nxt))
                        stack.append(nxt)
        return reach

    ref_before = before(ref)
    pred_before_mapped = {(m[a], m[b]) for (a, b) in before(pred) if a in m and b in m}
    pairs = [(a, b) for a in set(m.values()) for b in set(m.values()) if a != b]
    agree = sum(1 for (a, b) in pairs if ((a, b) in ref_before) == ((a, b) in pred_before_mapped))
    partial_order = agree / len(pairs) if pairs else 1.0

    exact = float(node_f1 == 1.0 and edge_f1 == 1.0)

    return {
        "node_f1": round(node_f1, 4),
        "edge_f1": round(edge_f1, 4),
        "partial_order_accuracy": round(partial_order, 4),
        "exact_match": bool(exact == 1.0),
        "matched_nodes": len(m),
        "pred_nodes": len(pred.nodes),
        "ref_nodes": len(ref.nodes),
    }

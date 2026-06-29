"""Join the static structure (code_arc) with the dynamic trace (calltrace) and
flatten the collapsed call tree into a numbered execution flow.

The flow is the DFS pre-order sequence of *mapped* runtime calls — calls that
resolve to a statically-known function via the ``(relpath, def_lineno)`` key
(the same key calltrace's ``collect_snippets`` uses). Consecutive steps become
numbered arrows; the first step is the program ENTRY, the last is the END.
Loop bodies are traversed once and carry a repeat count. External boundary
calls are collected separately (hidden by default in the UI).
"""

import os
from .tree import LoopNode


def _rel(filename, base):
    """Relative path of ``filename`` against ``base``; ``""`` for synthetic names."""
    if not filename or filename.startswith("<"):
        return ""
    try:
        return os.path.relpath(filename, base)
    except ValueError:
        return filename


def _pkg_hint(filename, base):
    """Best-effort top-level package name for an external file (for grouping)."""
    if not filename:
        return "external"
    norm = filename.replace("\\", "/")
    if "/site-packages/" in norm:
        tail = norm.split("/site-packages/", 1)[1]
        return tail.split("/", 1)[0]
    if "/dist-packages/" in norm:
        tail = norm.split("/dist-packages/", 1)[1]
        return tail.split("/", 1)[0]
    rel = _rel(filename, base)
    if rel:
        return rel.split(os.sep)[0]
    return "external"


def build_block_index(project_data, base):
    """Map ``(relpath, def_lineno) -> block full_name`` from static analysis.

    Also returns ``mod_by_rel`` mapping ``relpath -> module dotted name`` for
    the ``<module>`` runtime-frame fallback, and ``class_ids`` (the set of
    class full_names) so class-body execution frames can be skipped as flow
    steps (they fire at import time, before ``main()``, and would otherwise
    hijack the ENTRY marker).
    """
    idx = {}
    mod_by_rel = {}
    class_ids = set()
    for mod in project_data.modules:
        rel = _rel(mod.file_path, base)
        if rel:
            mod_by_rel[rel] = mod.name
        for f in mod.functions:
            if f.lineno:
                idx[(rel, f.lineno)] = f.full_name
        for c in mod.classes:
            if c.lineno:
                idx[(rel, c.lineno)] = c.full_name
                class_ids.add(c.full_name)
            for m in c.methods:
                if m.lineno:
                    idx[(rel, m.lineno)] = m.full_name
    return idx, mod_by_rel, class_ids


def _dedupe_edges(edges):
    """Merge duplicate (s, t) flow edges into a single edge with step range.

    When A calls B many times, only one arrow is drawn from A→B.  The label
    shows the step range ``first>last`` (e.g. ``3>2000``); for a single call
    the label is just the step number (e.g. ``42``).
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for e in edges:
        key = (e["s"], e["t"])
        if key not in groups:
            groups[key] = {"s": e["s"], "t": e["t"],
                           "step_first": e["step"], "step_last": e["step"],
                           "count": 1}
        else:
            g = groups[key]
            if e["step"] < g["step_first"]:
                g["step_first"] = e["step"]
            if e["step"] > g["step_last"]:
                g["step_last"] = e["step"]
            g["count"] += 1
    return list(groups.values())


def flatten_flow(root, block_index, base, flow_depth=None, class_ids=None):
    """Flatten the collapsed call tree into a numbered execution flow.

    Returns a dict with ``steps``, ``edges``, ``entry``, ``end``, ``external``
    and ``block_hits`` (per-block ``{count, duration}`` for every mapped call,
    including those beyond ``flow_depth`` so deep blocks still show as "hit").
    """
    class_ids = class_ids or set()
    steps = []          # [{id, step, depth, loop_count}]
    edges = []          # [{s, t, step}]
    block_hits = {}     # full_name -> {count, duration}
    external = {}       # key -> {label, pkg, count, duration, file}
    counter = [0]
    prev_id = [None]

    def hit(full_name, count, duration):
        rec = block_hits.get(full_name)
        if rec is None:
            block_hits[full_name] = {"count": count, "duration": duration}
        else:
            rec["count"] += count
            rec["duration"] += duration

    def add_external(node):
        pkg = _pkg_hint(node.filename, base)
        key = (pkg, node.name)
        rec = external.get(key)
        if rec is None:
            external[key] = {"label": node.name, "pkg": pkg,
                             "count": node.count, "duration": node.duration,
                             "file": node.filename}
        else:
            rec["count"] += node.count
            rec["duration"] += node.duration

    def visit(node, depth, pending):
        # pending: a one-element list [loop_count] consumed by the first mapped
        # step encountered within a loop body (so only one step carries ×N).
        if isinstance(node, LoopNode):
            # Body is at the same call depth as the loop; carry the loop count
            # onto the first mapped step inside (unless an outer loop already
            # set a pending count, in which case it wins).
            cell = pending if pending[0] > 0 else [node.count]
            for c in node.children:
                visit(c, depth, cell)
            return

        # CallNode
        if node.external:
            add_external(node)
            return

        full = block_index.get((_rel(node.filename, base), node.lineno))
        if full is None or full in class_ids:
            # Not a flow step: unmapped frames (<module>, dataclass-generated
            # __init__, lambda...) or class-body execution frames (which fire
            # at import time, before main(), and would mis-set the ENTRY
            # marker). Class hit-ness is derived from called methods instead.
            for c in node.children:
                visit(c, depth, pending)
            return

        hit(full, node.count, node.duration)

        if flow_depth is not None and depth > flow_depth:
            # Too deep to draw as a step; consume any pending loop badge so it
            # does not leak, then descend.
            pending[0] = 0
            for c in node.children:
                visit(c, depth + 1, [0])
            return

        counter[0] += 1
        step = counter[0]
        lc = pending[0]
        pending[0] = 0
        steps.append({"id": full, "step": step, "depth": depth,
                      "loop_count": lc if lc > 1 else 0})

        if prev_id[0] is not None and prev_id[0] != full:
            edges.append({"s": prev_id[0], "t": full, "step": step})
        prev_id[0] = full

        for c in node.children:
            visit(c, depth + 1, [0])

    for child in root.children:
        visit(child, 1, [0])

    entry_id = steps[0]["id"] if steps else None
    end_id = steps[-1]["id"] if steps else None
    edges = _dedupe_edges(edges)
    external_list = [
        {"id": "ext::%d" % i, "label": e["label"], "pkg": e["pkg"],
         "count": e["count"], "duration": e["duration"]}
        for i, e in enumerate(external.values())
    ]
    return {
        "steps": steps,
        "edges": edges,
        "entry": entry_id,
        "end": end_id,
        "external": external_list,
        "block_hits": block_hits,
    }

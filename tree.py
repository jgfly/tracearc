"""Call-tree data structures and loop collapsing.

The tracer builds a literal call tree (one node per function call). After the
run we *collapse* it: consecutive identical subtrees (the body of a loop) are
merged into a single node carrying a repeat count, and a fully periodic run of
children is wrapped in a ``LoopNode``. This keeps the report small and readable
even when a model generates hundreds of tokens through an identical code path.
"""

import os


class CallNode:
    """A single function call in the trace."""

    __slots__ = (
        "name", "filename", "lineno", "external",
        "children", "count", "duration", "t0", "_sig",
    )

    def __init__(self, name, filename, lineno, external=False):
        self.name = name
        self.filename = filename
        self.lineno = lineno
        self.external = external
        self.children = []
        self.count = 1          # how many times this exact subtree repeated
        self.duration = 0.0     # seconds spent (summed across repeats)
        self.t0 = 0.0
        self._sig = None

    def add_child(self, node):
        self.children.append(node)


class LoopNode:
    """A group of children that repeat as a block (a loop body)."""

    __slots__ = ("count", "children", "duration", "_sig",
                 "name", "external", "filename", "lineno")

    def __init__(self, count, children):
        self.count = count
        self.children = children
        self.duration = 0.0
        self._sig = None
        self.name = "loop"
        self.external = False
        self.filename = ""
        self.lineno = 0


def _relpath(filename, base):
    if not filename or filename.startswith("<"):
        return filename or ""
    try:
        return os.path.relpath(filename, base)
    except ValueError:
        return filename


def signature(node, base=""):
    """A structural fingerprint of a node, used to detect identical loop bodies.

    Intentionally excludes ``count`` so that ``[A x2, A x1]`` still merges into
    ``A x3``.
    """
    if node._sig is not None:
        return node._sig
    body = ",".join(signature(c, base) for c in node.children)
    if isinstance(node, LoopNode):
        s = f"L[{body}]"
    else:
        rel = _relpath(node.filename, base)
        s = f"C({node.name}|{rel}|{node.lineno}|{int(node.external)})[{body}]"
    node._sig = s
    return s


def _merge_into(a, b):
    """Fold sibling ``b`` into ``a`` (structurally identical)."""
    a.count += b.count
    a.duration += b.duration


def collapse(node, base=""):
    """Recursively collapse repeated subtrees / loop bodies in ``node``."""
    for c in node.children:
        collapse(c, base)

    children = node.children

    # 1) run-length merge consecutive identical children
    merged = []
    for c in children:
        if merged and signature(merged[-1], base) == signature(c, base):
            _merge_into(merged[-1], c)
        else:
            merged.append(c)
    children = merged

    # 2) wrap a fully periodic run of children in a LoopNode
    n = len(children)
    if n >= 4:
        for p in range(1, n // 2 + 1):
            if n % p == 0 and all(
                signature(children[i], base) == signature(children[i % p], base)
                for i in range(n)
            ):
                k = n // p
                body = [children[j] for j in range(p)]
                lp = LoopNode(count=k, children=body)
                lp.duration = sum(c.duration for c in body) * k
                children = [lp]
                break

    node.children = children


def count_nodes(node):
    total = 1
    for c in node.children:
        total += count_nodes(c)
    return total


def max_depth(node, depth=0):
    if not node.children:
        return depth
    return max(max_depth(c, depth + 1) for c in node.children)

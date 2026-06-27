"""The sys.settrace-based tracer that builds the call tree.

How tracing actually works (and the gotcha that shapes this design)
------------------------------------------------------------------
* ``sys.settrace`` installs a *global* trace function that fires a ``'call'``
  event for **every** new Python frame in the thread -- regardless of what the
  trace function returns. Returning ``None`` only disables line/return events
  for that one frame; it does *not* stop the global trace from firing for its
  callees. (Returning ``None`` does, however, stop C-level ``c_call`` noise --
  settrace never reports C functions at all, which is why heavy torch ops incur
  no per-op trace cost.)

* ``frame.f_trace_lines = False`` suppresses per-line events for a frame, so a
  traced frame costs only one ``'call'`` + one ``'return'`` -- not one event
  per executed line.

Consequences for the design
---------------------------
* We cannot "skip into" a package by returning None -- the global trace still
  sees every nested call. Instead we decide per call, using the frame's own
  filename and its caller (``frame.f_back``):
    - project frame (under a step-in dir): record a node and trace its return.
    - external frame whose *caller* is a project frame: record a single leaf
      (the boundary call) and do not descend.
    - external frame whose caller is also external: skip entirely (it is an
      internal detail of an external package).

* Parentage is recovered by walking ``frame.f_back`` to the nearest project
  ancestor frame (looked up in a per-thread ``id(frame) -> node`` map). This
  avoids maintaining a fragile parallel stack and naturally handles exceptions.

* Threading: each thread gets its own node map and "started" flag (via
  ``threading.local``). Tree mutation is guarded by a lock. Worker threads are
  captured; their top project calls appear as separate branches under root.

* Frames before the first project frame (runpy / our own runner / import
  bootstrap) are skipped via a per-thread ``started`` flag so the report's root
  children are the script's own calls.
"""

import os
import time
import threading

from .tree import CallNode
from . import filters


# Cached result of the torch._dynamo.utils.is_compiling lookup.
# None = not yet resolved; True/False = resolved.
_DYNAMO_IS_COMPILING = None


def _in_dynamo():
    """Return True when torch._dynamo is currently tracing Python bytecode.

    Dynamo walks the bytecode of the function it is compiling.  If our
    ``sys.settrace`` hook is active at that moment, Dynamo follows it into
    ``_local_trace`` → ``_on_return`` → ``time.perf_counter()``, which is a
    C builtin Dynamo cannot trace, and raises ``Unsupported``.  Bailing out
    when Dynamo is active avoids the crash entirely.
    """
    global _DYNAMO_IS_COMPILING
    if _DYNAMO_IS_COMPILING is None:
        try:
            import torch._dynamo.utils as _du
            _DYNAMO_IS_COMPILING = _du.is_compiling
        except Exception:
            # torch not installed, or import failed — Dynamo is not active.
            _DYNAMO_IS_COMPILING = False
    if _DYNAMO_IS_COMPILING is False:
        return False
    return _DYNAMO_IS_COMPILING()


class Tracer:
    def __init__(self, step_in_dirs, step_all_imports, record_external=True):
        self.step_in_dirs = [filters._norm(d) for d in step_in_dirs if d]
        self.step_all_imports = step_all_imports
        self.record_external = record_external
        self.exclude_dirs = []          # never stepped into (e.g. calltrace itself)
        self.root = CallNode(name="<root>", filename="", lineno=0)
        self.raw_call_count = 0
        self._tl = threading.local()
        self._lock = threading.Lock()
        self._step_cache = {}

    def _now(self):
        """Return a monotonic timestamp, or 0.0 when Dynamo is compiling.

        ``time.perf_counter()`` is a C builtin that torch._dynamo cannot trace.
        When Dynamo is actively compiling we must not call it, or Dynamo raises
        ``Unsupported``.  Returning 0.0 is safe because we also bail out of
        tracing entirely during Dynamo compilation (see ``global_trace`` and
        ``_local_trace``), so no node is ever created with a stale ``t0``.
        """
        if _in_dynamo():
            return 0.0
        return time.perf_counter()

    # -- thread-local state -------------------------------------------------

    def _nodes(self):
        d = getattr(self._tl, "nodes", None)
        if d is None:
            d = {}
            self._tl.nodes = d
        return d

    def _started(self):
        return getattr(self._tl, "started", False)

    # -- path decisions -----------------------------------------------------

    def _excluded(self, fnorm):
        for d in self.exclude_dirs:
            if fnorm == d or fnorm.startswith(d + os.sep):
                return True
        return False

    def _compute_step(self, filename):
        if not filename or filename.startswith("<"):
            return False
        f = filters._norm(filename)
        if self._excluded(f):
            return False
        # Explicit --project dirs have highest priority: if the user named a
        # directory we always step into it, even when it lives inside
        # site-packages (e.g. ``--project …/site-packages/vllm``).
        for d in self.step_in_dirs:
            if f == d or f.startswith(d + os.sep):
                return True
        # Anything in site-packages is a third-party dependency, never "the
        # project" -- even when the venv physically lives under the project
        # directory (e.g. ./project/.venv). Only descend with --step-all-imports.
        if filters.is_sitepackage(filename):
            return self.step_all_imports
        if self.step_all_imports and not filters.is_stdlib(filename):
            return True
        return False

    def _should_step_in(self, filename):
        v = self._step_cache.get(filename)
        if v is None:
            v = self._compute_step(filename)
            self._step_cache[filename] = v
        return v

    def _parent_node(self, frame):
        """Nearest recorded project ancestor of ``frame`` (or root)."""
        nodes = self._nodes()
        f = frame.f_back
        while f is not None:
            node = nodes.get(id(f))
            if node is not None:
                return node
            f = f.f_back
        return self.root

    # -- trace events -------------------------------------------------------

    def _on_call(self, frame):
        # Suppress per-line events; we only need call/return.
        frame.f_trace_lines = False

        code = frame.f_code
        filename = code.co_filename
        name = getattr(code, "co_qualname", code.co_name)
        lineno = code.co_firstlineno
        step = self._should_step_in(filename)

        if step:
            self._tl.started = True
            node = CallNode(name=name, filename=filename, lineno=lineno, external=False)
            node.t0 = self._now()
            parent = self._parent_node(frame)
            with self._lock:
                parent.add_child(node)
                self.raw_call_count += 1
            self._nodes()[id(frame)] = node
            return self._local_trace

        # External frame.
        if not self._started() or not self.record_external:
            return None
        # Record only the boundary call: caller must be a project frame.
        caller = frame.f_back
        parent_node = self._nodes().get(id(caller)) if caller is not None else None
        if parent_node is not None:
            node = CallNode(name=name, filename=filename, lineno=lineno, external=True)
            with self._lock:
                parent_node.add_child(node)
                self.raw_call_count += 1
        return None

    def _on_return(self, frame):
        node = self._nodes().pop(id(frame), None)
        if node is not None:
            node.duration += self._now() - node.t0

    # -- trace functions ----------------------------------------------------

    def global_trace(self, frame, event, arg):
        if event == "call":
            # When torch._dynamo is compiling, any active sys.settrace hook
            # interferes with Dynamo's own bytecode walking.  Bail out so
            # Dynamo never follows us into time.perf_counter (a C builtin
            # it cannot trace).
            if _in_dynamo():
                return None
            return self._on_call(frame)
        return None

    def _local_trace(self, frame, event, arg):
        if _in_dynamo():
            return None
        if event == "return":
            self._on_return(frame)
            return self._local_trace
        return self._local_trace

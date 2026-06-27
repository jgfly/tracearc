"""Run a target script under the tracer and emit the tracearc HTML report.

Pipeline (each stage runs in order):
  1. Static analysis  — code_arc's AST analyzer parses the project dir. Done
     *before* the tracer is installed, because the analyzer uses a process pool
     that must not interact with ``sys.settrace``.
  2. Dynamic trace     — calltrace's ``sys.settrace`` engine runs the entry
     script and builds a collapsed call tree (``CallNode``/``LoopNode``).
  3. Join + flatten    — ``flow.flatten_flow`` maps each runtime call to a
     static block via ``(relpath, def_lineno)`` and produces the numbered
     execution flow (steps / edges / entry / end / loops / external).
  4. Generate          — the (adapted code_arc) HTML generator renders the
     block graph with the numbered flow arrows overlaid.
"""

import os
import sys
import time
import runpy
import threading
import multiprocessing
import multiprocessing.process

from .tracer import Tracer
from .tree import collapse, count_nodes, max_depth
from .analyzer import CodeAnalyzer
from .flow import build_block_index, flatten_flow
from .generator import HTMLGenerator
from .source import SourceServer
from . import filters


def patch_multiprocessing():
    """Prevent child processes from inheriting the sys.settrace hook.

    Frameworks like vLLM spawn worker subprocesses; if a child inherits our
    hook and later calls ``torch.compile`` (Dynamo), Dynamo walks the bytecode,
    hits the trace function, follows it into ``time.perf_counter()`` (a C
    builtin it cannot trace) and crashes. We clear the hook around ``start()``
    (spawn) and via ``register_at_fork`` (fork).
    """
    _orig_start = multiprocessing.process.BaseProcess.start

    def _patched_start(self, *args, **kwargs):
        old_trace = sys.gettrace()
        old_thread_trace = threading.gettrace()
        sys.settrace(None)
        threading.settrace(None)
        try:
            return _orig_start(self, *args, **kwargs)
        finally:
            sys.settrace(old_trace)
            threading.settrace(old_thread_trace)

    multiprocessing.process.BaseProcess.start = _patched_start

    if hasattr(os, "register_at_fork"):
        def _clear_after_fork():
            sys.settrace(None)
            threading.settrace(None)
        try:
            os.register_at_fork(after_in_child=_clear_after_fork)
        except (OSError, RuntimeError):
            pass


def run(script, script_args, step_in_dirs, project_dir, step_all_imports,
        output, collapse_loops=True, record_external=True, flow_depth=None,
        static_edges=True, base_dir=None, serve=False, port=None, title=None):
    script = os.path.abspath(script)
    script_dir = os.path.dirname(script)

    sys.argv = [script] + list(script_args)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    # base: the directory relpaths are computed against (must be the same for
    # the static analyzer and the dynamic tracer so the join key matches).
    base = os.path.abspath(base_dir or project_dir or script_dir)

    # ---- 1. static analysis (before the tracer is installed) --------------
    project_data = CodeAnalyzer(project_dir).analyze()
    if not static_edges:
        project_data.call_edges = []
        project_data.class_inheritance = []
    block_index, _mod_by_rel, class_ids = build_block_index(project_data, base)

    # ---- 2. dynamic trace -------------------------------------------------
    tracer = Tracer(step_in_dirs, step_all_imports, record_external=record_external)
    # Never trace into the tracearc tool's own source files. (Exclude only the
    # package's root .py files, not subdirectories like examples/, so bundled
    # test projects remain traceable.)
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in os.listdir(here):
        if fname.endswith(".py"):
            tracer.exclude_dirs.append(filters._norm(os.path.join(here, fname)))

    # Prevent child processes from inheriting sys.settrace (would crash
    # torch.compile / Dynamo inside them).
    patch_multiprocessing()

    crashed = False
    exit_code = 0
    start = time.perf_counter()
    sys.settrace(tracer.global_trace)
    threading.settrace(tracer.global_trace)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        if exit_code != 0:
            crashed = True
    except BaseException:
        # Script crashed — still render whatever we captured, then re-raise so
        # the user sees the original traceback. END marks the crash point.
        crashed = True
        sys.settrace(None)
        threading.settrace(None)
        elapsed = time.perf_counter() - start
        _emit(tracer, project_data, block_index, class_ids, base, script, output,
              elapsed, collapse_loops, flow_depth, serve, port, title, crashed)
        raise
    finally:
        sys.settrace(None)
        threading.settrace(None)

    elapsed = time.perf_counter() - start
    _emit(tracer, project_data, block_index, class_ids, base, script, output,
          elapsed, collapse_loops, flow_depth, serve, port, title, crashed)
    return exit_code


def _emit(tracer, project_data, block_index, class_ids, base, script, output,
          elapsed, collapse_loops, flow_depth, serve, port, title, crashed):
    root = tracer.root
    if collapse_loops:
        collapse(root, base=base)
    collapsed_nodes = count_nodes(root)
    depth = max_depth(root)

    flow = flatten_flow(root, block_index, base, flow_depth=flow_depth,
                        class_ids=class_ids)
    meta = {
        "raw_calls": tracer.raw_call_count,
        "elapsed": elapsed,
        "collapsed_nodes": collapsed_nodes,
        "max_depth": depth,
        "crashed": crashed,
    }

    if title is None:
        title = os.path.basename(os.path.dirname(script)) or "tracearc"

    html = HTMLGenerator(project_data, title=title, flow=flow, meta=meta).generate()

    if not serve:
        with open(output, "w", encoding="utf-8") as f:
            f.write(html)
        return

    server = SourceServer(base, tracer.step_in_dirs, html=html, port=port or 0)
    with open(output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[tracearc] serving report at {server.url} (Ctrl+C to stop)",
          file=sys.stderr)
    server.serve_forever(open_browser=True)

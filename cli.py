"""Command-line entry point for tracearc."""

import argparse
import os
import sys

from .runner import run


def build_parser():
    p = argparse.ArgumentParser(
        prog="tracearc",
        description=(
            "Trace a Python script's runtime call stack and render it as a "
            "code_arc-style block graph with numbered execution-order arrows. "
            "Loops collapse to a repeat count; external calls are hidden by "
            "default; static call/inherit edges are off by default."
        ),
    )
    p.add_argument("script", help="Entry script to run (e.g. example.py).")
    p.add_argument("script_args", nargs=argparse.REMAINDER,
                   help="Arguments forwarded verbatim to SCRIPT.")
    p.add_argument("--project", action="append", default=[], metavar="DIR",
                   help="Directory to statically analyze AND step into at runtime "
                        "(repeatable; the first is the analysis root). "
                        "Default: the directory containing SCRIPT.")
    p.add_argument("--step-all-imports", action="store_true",
                   help="Also step into every non-stdlib import (torch, ...).")
    p.add_argument("--no-external-leaves", action="store_true",
                   help="Do not record external (out-of-project) calls at all.")
    p.add_argument("--no-collapse", action="store_true",
                   help="Disable loop collapsing; use the raw call tree.")
    p.add_argument("--flow-depth", type=int, default=8, metavar="N",
                   help="Max call-tree depth for execution arrows (default 8; "
                        "0 = unlimited). Deeper calls still mark their blocks as hit.")
    p.add_argument("--no-static-edges", action="store_true",
                   help="Omit static call/inherit edges entirely (smaller HTML).")
    p.add_argument("-o", "--output", default=None,
                   help="Output HTML path (default: ./tracearc_report.html).")
    p.add_argument("--serve", action="store_true",
                   help="Serve the report on a local HTTP server and open a browser.")
    p.add_argument("--port", type=int, default=0,
                   help="Port for --serve (default 0 = auto free port).")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    script = os.path.abspath(args.script)
    if not os.path.isfile(script):
        print(f"tracearc: script not found: {script}", file=sys.stderr)
        return 2

    script_dir = os.path.dirname(script)
    step_in_dirs = [os.path.abspath(d) for d in args.project] or [script_dir]
    if script_dir not in step_in_dirs:
        step_in_dirs.append(script_dir)
    project_dir = os.path.abspath(args.project[0]) if args.project else script_dir

    output = args.output or os.path.join(os.getcwd(), "tracearc_report.html")
    flow_depth = args.flow_depth if args.flow_depth and args.flow_depth > 0 else None

    exit_code = run(
        script=script,
        script_args=args.script_args,
        step_in_dirs=step_in_dirs,
        project_dir=project_dir,
        step_all_imports=args.step_all_imports,
        output=output,
        collapse_loops=not args.no_collapse,
        record_external=not args.no_external_leaves,
        flow_depth=flow_depth,
        static_edges=not args.no_static_edges,
        serve=args.serve,
        port=args.port,
    )
    print(f"[tracearc] report written to: {output}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

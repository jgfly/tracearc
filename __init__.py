"""
tracearc — runtime call-stack visualizer.

Combines calltrace (dynamic sys.settrace tracer + loop collapsing) with
code_arc (static AST block-graph UI). Runs an entry script, captures its
runtime call stack, and renders an interactive HTML graph where the primary
edges are numbered execution-order arrows (entry -> ... -> end), loops are
collapsed, external calls are hidden by default, and code_arc's static
call/inherit edges are present but off by default.

Usage:
    python -m tracearc [--project DIR]... [--flow-depth N] ENTRY_SCRIPT [ARGS...]
"""

from .analyzer import CodeAnalyzer
from .generator import HTMLGenerator
from .runner import run
from .cli import main

__all__ = ["CodeAnalyzer", "HTMLGenerator", "run", "main"]

# tracearc

**Runtime call-stack visualizer** — combines [calltrace](../calltrace)'s dynamic
`sys.settrace` tracer (real execution, loop collapsing, multiprocessing/Dynamo
safety) with [code_arc](../code_arc)'s static-AST block-graph UI (packages /
modules / classes / functions as colored blocks, SVG edges, minimap, source
panel, zoom/pan/search).

Run an entry script; get an interactive HTML graph where the **primary edges
are numbered execution-order arrows** tracing the actual call stack from program
entry ▶ to the last call ■. Loops collapse to a single body + `×N` badge;
external (out-of-project) calls are hidden by default; code_arc's static
call/inherit edges are present but **off by default** to avoid clutter.

## What you get

- **Numbered flow arrows** (amber) — the real runtime call sequence, each
  arrow labeled with its step number. The first step's block is the program
  **ENTRY** (green ▶ ring + badge); the last is the **END** (red ■).
- **Loop simplification** — repeated loop bodies (including `while`-loop
  check+body sequences) collapse to one pass with a `↻×N` badge, instead of
  one arrow per iteration. Keeps even 500-token generation readable.
- **External calls hidden by default** — out-of-project calls (torch,
  transformers, stdlib) don't expand into the graph; toggle `External` to see
  them as a dimmed summary list with counts.
- **Static edges off by default** — code_arc's call (blue) and inheritance
  (dashed red) edges are available but turned off; toggle `Calls`/`Inherit`/
  `Edges` to overlay the static architecture.
- **Uncalled blocks dimmed** — every statically-known function/method that was
  *not* hit at runtime is dimmed; `Hide uncalled` hides them entirely.
- Full code_arc UI retained: nested block grid layout, SVG bezier edges,
  minimap, slide-in source panel (click a block), search, zoom/pan/fit,
  hover-to-highlight + click-to-lock, per-block connection list.

## Install / run

No install — run as a module with any Python that has the target's deps:

```bash
# Trace a script, write a self-contained HTML (offline; source embedded)
python -m tracearc [--project DIR]... [--flow-depth N] ENTRY_SCRIPT [SCRIPT_ARGS...]
```

By default the script's own directory is both the **static analysis root** and
the **runtime step-into root** (only project code is traced into; imports like
torch/transformers are recorded as external boundaries, not descended).

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--project DIR` | script dir | Directory to statically analyze AND step into at runtime (repeatable; first is the analysis root) |
| `--step-all-imports` | off | Also descend into non-stdlib imports (torch, …) |
| `--no-external-leaves` | off | Don't record external calls at all |
| `--no-collapse` | off | Disable loop collapsing (raw tree) |
| `--flow-depth N` | 8 | Max call-tree depth for execution arrows (0 = unlimited; deeper calls still mark their blocks hit) |
| `--no-static-edges` | off | Omit static call/inherit edges (smaller HTML) |
| `-o FILE` | `./tracearc_report.html` | Output path |
| `--serve` / `--port` | off | Serve via local HTTP server, open a browser |

### Examples

```bash
# Mini demo (synthetic nano-vllm-like structure; runs in milliseconds)
python -m tracearc -o mini_report.html --project tracearc/examples/mini tracearc/examples/mini/main.py

# Real nano-vllm inference (~80s on GPU)
python -m tracearc -o nano.html --project /path/to/nano-vllm /path/to/nano-vllm/example.py
```

## How it works

Four stages, in order:

1. **Static analysis** (`analyzer.py`, from code_arc) — AST-parse the project
   dir into modules/classes/functions with source + call/inherit edges + a
   package tree. Build a lookup `{(relpath, def_lineno) → block full_name}`.
   Runs *before* the tracer is installed (the analyzer uses a process pool).
2. **Dynamic trace** (`tracer.py` + `tree.py` + `filters.py`, from calltrace) —
   `sys.settrace` + `threading.settrace`, `runpy.run_path(entry, "__main__")`,
   then `collapse(root)`. Crashes are caught: whatever was captured still
   renders, with END marking the crash point.
3. **Join + flatten** (`flow.py`, new) — DFS pre-order over the collapsed
   tree, mapping each `CallNode` to a static block via the `(relpath,
   def_lineno)` key (the same key calltrace's snippet extractor uses). Produces
   numbered flow steps + sequential edges; loop bodies traversed once; external
   boundary calls collected separately; per-block hit/count/duration merged
   onto the static node JSON.
4. **Generate** (`generator.py`, adapted from code_arc) — one self-contained
   HTML. Reuses code_arc's CSS, layout, SVG edge machinery, minimap, source
   panel, highlight model. Adds amber numbered flow arrows (with step-number
   badges), entry/end markers, loop/external/dimming, and toolbar toggles.

The join is natural: a runtime frame's `(co_filename, co_firstlineno)` is the
same `(relpath, def_lineno)` the static analyzer records for each `def`/`class`.

## Project layout

| File | Origin |
|---|---|
| `tracer.py` `tree.py` `filters.py` `source.py` | from calltrace (engine + loop collapse; `tree.py` adds a relaxed `while`-loop prefix collapse) |
| `analyzer.py` | from code_arc (static AST analysis) |
| `flow.py` | new (join + flatten) |
| `generator.py` | adapted from code_arc (flow arrows, entry/end, loops, external, dimming, toggles) |
| `runner.py` `cli.py` `__init__.py` `__main__.py` | orchestration + CLI |
| `examples/mini/` | tiny synthetic test project (fast dev loop) |

## Toolbar reference

`Fit` · `Flow` (on) · `Entry/End` (on) · `External` (off) · `Hide uncalled` (off) ·
`Edges` (off) · `Calls` (off) · `Inherit` (off) · `pkg-level` · `Edges Top` ·
`Collapse` · max static edges · search box. Keyboard: `F` fit, `/` search, `Esc` clear.

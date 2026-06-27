"""Source-code helpers: snippet extraction and an on-demand HTTP source server.

Two ways the report reaches source code:

* **Embedded (default)** — ``collect_snippets`` walks the call tree and, for each
  unique ``(file, def-line)``, pulls just that function's body out of the file.
  The renderer bakes the resulting map into the HTML, so hovering works offline
  with no server. Only the referenced functions are embedded -- never whole files
  -- so the HTML stays small.

* **Served (``--serve``)** — ``SourceServer`` serves the report itself at ``/``
  and a ``/src`` endpoint that returns a source window on demand. The HTML then
  carries no embedded code at all and fetches lazily on hover. Same-origin serving
  keeps ``fetch()`` free of CORS / mixed-content headaches.

``lineno`` throughout is ``co_firstlineno`` -- the ``def``/``class`` line -- so
"highlight the definition line" is just "highlight ``lineno``".
"""

import os
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .tree import CallNode, LoopNode
from . import filters


# --------------------------------------------------------------------------
# snippet extraction
# --------------------------------------------------------------------------

def _read_lines(filename):
    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except (OSError, ValueError):
        return None


def _indent_width(line):
    """Leading-whitespace width of a line (tabs count as the rest to the next 8)."""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 8 - (n % 8)
        else:
            break
    return n


def extract_function_source(filename, def_lineno, max_lines=60):
    """Return the source text of the function/class defined at ``def_lineno``.

    Captures from ``def_lineno`` until the body dedents back to (or below) the
    def line's own indent -- i.e. the next sibling/top-level statement -- capped
    at ``max_lines``. Returns ``None`` if the file can't be read or the line is
    out of range. No parsing/importing: just indentation, which is robust for the
    ``def``/``class`` lines ``co_firstlineno`` yields.
    """
    lines = _read_lines(filename)
    if not lines:
        return None
    idx = def_lineno - 1
    if idx < 0 or idx >= len(lines):
        return None

    def_indent = _indent_width(lines[idx])
    body = [lines[idx]]
    seen_body = False
    i = idx + 1
    while i < len(lines) and (i - idx) < max_lines:
        line = lines[i]
        stripped = line.strip()
        if stripped:
            ind = _indent_width(line)
            if seen_body and ind <= def_indent and not stripped.startswith("#"):
                # Dedented back to/below the def: body is over.
                break
            if ind > def_indent:
                seen_body = True
            elif stripped.startswith("#"):
                # A comment at the def's indent between def and body: keep scanning.
                pass
            elif not seen_body:
                # Still on the signature (multi-line def / decorators / arg list).
                pass
            else:
                break
        body.append(line)
        i += 1
    return "\n".join(body).rstrip("\n")


def collect_snippets(root, base):
    """Walk ``root`` and collect per-function source snippets.

    Returns ``{relpath: {lineno: source_text}}``. The same function called many
    times collapses to a single entry (keyed by file+line). Unreadable files are
    skipped silently.
    """
    out = {}

    def visit(node):
        if not isinstance(node, LoopNode) and node.filename and node.lineno:
            if not node.filename.startswith("<"):
                try:
                    rel = os.path.relpath(node.filename, base)
                except ValueError:
                    rel = node.filename
                key = rel
                if key not in out:
                    out[key] = {}
                if node.lineno not in out[key]:
                    src = extract_function_source(node.filename, node.lineno)
                    if src is not None:
                        out[key][node.lineno] = src
        for c in node.children:
            visit(c)

    visit(root)
    return out


# --------------------------------------------------------------------------
# on-demand source server ( --serve )
# --------------------------------------------------------------------------

def find_free_port(host="127.0.0.1"):
    """Return a free TCP port on ``host`` (binds to 0, then releases)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


class SourceServer:
    """Serves the rendered report at ``/`` and source windows at ``/src``.

    Only files living under ``base`` or one of ``step_in_dirs`` may be served --
    everything else (and any ``..`` traversal) is rejected with 404.

    The listening socket is bound in ``__init__`` (so the chosen port is known
    before the HTML is rendered and can be baked into it as the fetch base URL).
    Call ``serve_forever()`` to actually handle requests.
    """

    def __init__(self, base, step_in_dirs, html="", port=0, host="127.0.0.1"):
        self.base = filters._norm(base)
        self.roots = [self.base] + [filters._norm(d) for d in step_in_dirs]
        self.html = html
        self.host = host
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # quiet

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    body = (server.html or "").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/src":
                    q = parse_qs(parsed.query)
                    rel = q.get("file", [""])[0]
                    line = q.get("line", ["1"])[0]
                    ctx = q.get("context", ["12"])[0]
                    try:
                        ctx = int(ctx)
                    except ValueError:
                        ctx = 12
                    payload = server._src_payload(rel, line, ctx)
                    if payload is None:
                        self._json({"error": "source unavailable"}, 404)
                    else:
                        self._json(payload)
                    return
                self.send_error(404)

            def do_OPTIONS(self):
                # Permissive CORS so a file://-opened copy could also fetch.
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET")
                self.end_headers()

        self._httpd = ThreadingHTTPServer((self.host, port), Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://{self.host}:{self.port}/"

    def _under_roots(self, path):
        p = filters._norm(path)
        for r in self.roots:
            if p == r or p.startswith(r + os.sep):
                return True
        return False

    def _resolve(self, rel):
        """Resolve a relpath against the roots; return the real path or None.

        Tries each root; the resolved real path must still live under a root
        (checked after symlink normalization), which is the actual security
        boundary -- so a relpath containing ``..`` is fine as long as it lands
        inside a served root, and ``../../etc/passwd`` is rejected.
        """
        for r in self.roots:
            cand = os.path.normpath(os.path.join(r, rel))
            if self._under_roots(cand):
                return filters._norm(cand)
        return None

    def _src_payload(self, rel, line, context=12):
        path = self._resolve(rel)
        if path is None:
            return None
        line = max(1, int(line))
        snippet = extract_function_source(path, line, max_lines=context * 3 or 60)
        if snippet is None:
            return None
        lines = snippet.split("\n")
        return {"start": line, "lines": lines}

    def serve_forever(self, open_browser=True):
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(self.url)
            except Exception:
                pass
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._httpd.server_close()
        return self.url

"""
HTML visualization generator.

Generates a self-contained interactive HTML file with:
- Module/Class/Function blocks with distinct colors, arranged in a loose 2D layout
- Function-granularity call edges with arrows at exact block edges
- Hover-to-highlight + click-to-lock edge highlighting with z-index management
- Click-to-view source code, Zoom/pan, Search, Filter
- Navigation minimap for quick positioning
"""

import html
import json

from .analyzer import ClassInfo, FuncInfo, ModuleInfo, PackageInfo, ProjectData


class HTMLGenerator:
    def __init__(self, project_data: ProjectData, title: str = "Code Arc",
                 flow: dict | None = None, meta: dict | None = None):
        self.data = project_data
        self.title = title
        # flow: result of flow.flatten_flow — {steps, edges, entry, end, external,
        # block_hits}. May be empty (pure static view, no runtime trace).
        self.flow = flow or {}
        # meta: run statistics — {raw_calls, elapsed, collapsed_nodes, max_depth,
        # crashed (bool: did the run crash instead of finishing)}.
        self.meta = meta or {}
        self._hits = self.flow.get("block_hits", {})

    def generate(self) -> str:
        nodes_json = self._generate_nodes_json()
        edges_json = self._generate_edges_json()
        inheritance_json = self._generate_inheritance_json()
        source_json = self._generate_source_json()
        flow_json = self._generate_flow_json()

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(self.title)} - tracearc</title>
<style>
{self._get_css()}
</style>
</head>
<body class="edges-on-top">
{self._get_html_body(nodes_json, edges_json, inheritance_json, source_json, flow_json)}
<script>
{self._get_javascript()}
</script>
</body>
</html>"""

    def _generate_nodes_json(self) -> str:
        if self.data.root_package:
            return json.dumps(
                self._package_to_json(self.data.root_package),
                ensure_ascii=False,
            )
        # Fallback: flat module list (if no package tree)
        nodes = []
        for module in self.data.modules:
            nodes.append(self._module_to_json(module))
        return json.dumps(nodes, ensure_ascii=False)

    def _package_to_json(self, pkg: PackageInfo) -> dict:
        node = {
            "id": pkg.name, "type": "package", "name": pkg.name,
            "label": pkg.name.split(".")[-1] or pkg.name,
            "full_name": pkg.name, "is_leaf": pkg.is_leaf, "children": [],
        }
        for child in pkg.children:
            if isinstance(child, PackageInfo):
                node["children"].append(self._package_to_json(child))
            elif isinstance(child, ModuleInfo):
                node["children"].append(self._module_to_json(child))
        return node

    def _module_to_json(self, module: ModuleInfo) -> dict:
        # __init__.py shares its dotted name with its package; give it a distinct id
        # so package and module DOM nodes don't collide in EM[id].
        is_init = module.file_path.replace("\\", "/").endswith("/__init__.py")
        node_id = module.name + ".__init__" if is_init else module.name
        label = "__init__" if is_init else (module.name.split(".")[-1] or module.name)
        module_node = {"id": node_id, "type": "module", "name": module.name,
            "label": label,
            "full_name": module.name, "file_path": module.file_path, "children": []}
        for cls in module.classes:
            class_node = {"id": cls.full_name, "type": "class", "name": cls.name,
                "full_name": cls.full_name, "init_params": cls.init_params,
                "bases": cls.bases, "lineno": cls.lineno, "children": [],
                **self._rt_class(cls)}
            for method in cls.methods:
                class_node["children"].append({"id": method.full_name, "type": "method",
                    "name": method.name, "full_name": method.full_name,
                    "params": method.params, "return_type": method.return_type,
                    "lineno": method.lineno, **self._rt(method.full_name)})
            module_node["children"].append(class_node)
        for func in module.functions:
            module_node["children"].append({"id": func.full_name, "type": "function",
                "name": func.name, "full_name": func.full_name,
                "params": func.params, "return_type": func.return_type,
                "lineno": func.lineno, **self._rt(func.full_name)})
        return module_node

    def _rt(self, full_name: str) -> dict:
        """Runtime annotation merged onto every class/method/function node:
        hit (was it called), count, duration, is_entry, is_end."""
        h = self._hits.get(full_name)
        return {
            "hit": bool(h),
            "count": h["count"] if h else 0,
            "duration": round(h["duration"], 6) if h else 0.0,
            "is_entry": full_name == self.flow.get("entry"),
            "is_end": full_name == self.flow.get("end"),
        }

    def _rt_class(self, cls) -> dict:
        """A class is "hit" if any of its methods was called at runtime; its
        count/duration aggregate the called methods. (Constructors map to the
        ``__init__`` def line, not the class def line, so the class itself is
        never directly in block_hits.)"""
        hits = self._hits
        called = [m for m in cls.methods if m.full_name in hits]
        count = sum(hits[m.full_name]["count"] for m in called)
        dur = sum(hits[m.full_name]["duration"] for m in called)
        return {
            "hit": bool(called),
            "count": count,
            "duration": round(dur, 6),
            "is_entry": cls.full_name == self.flow.get("entry"),
            "is_end": cls.full_name == self.flow.get("end"),
        }

    def _generate_flow_json(self) -> str:
        """The numbered execution flow: steps, sequential edges (each labeled
        with the destination step number), entry/end ids, and external
        boundary-call summaries (hidden by default in the UI)."""
        if not self.flow:
            return json.dumps({"steps": [], "edges": [], "entry": None,
                               "end": None, "external": [], "crashed": False},
                              ensure_ascii=False)
        return json.dumps({
            "steps": self.flow.get("steps", []),
            "edges": self.flow.get("edges", []),
            "entry": self.flow.get("entry"),
            "end": self.flow.get("end"),
            "external": self.flow.get("external", []),
            "crashed": self.meta.get("crashed", False),
        }, ensure_ascii=False)

    def _generate_edges_json(self) -> str:
        return json.dumps([{"source": s, "target": t, "type": "call"}
                           for s, t in self.data.call_edges], ensure_ascii=False)

    def _generate_inheritance_json(self) -> str:
        return json.dumps([{"source": s, "target": t, "type": "inherit"}
                           for s, t in self.data.class_inheritance], ensure_ascii=False)

    def _generate_source_json(self) -> str:
        sources = {}
        for m in self.data.modules:
            for c in m.classes:
                sources[c.full_name] = c.source_code
                for mt in c.methods:
                    sources[mt.full_name] = mt.source_code
            for f in m.functions:
                sources[f.full_name] = f.source_code
        return json.dumps(sources, ensure_ascii=False)

    def _get_css(self) -> str:
        return """
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI','Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  background:#0d0e1a;color:#e0e0e0;overflow:hidden;height:100vh;width:100vw}

#toolbar{position:fixed;top:0;left:0;right:0;height:50px;
  background:linear-gradient(180deg,#14152a,#10112a);border-bottom:1px solid #222340;
  display:flex;align-items:center;padding:0 20px;z-index:100;gap:10px}
#toolbar .logo{font-size:16px;font-weight:800;
  background:linear-gradient(135deg,#64b5f6,#9c7cff);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:1px}
#toolbar .project-name{font-size:13px;color:#555570;
  border-left:1px solid #2a2b45;padding-left:12px;margin-left:4px}
#search-box{margin-left:auto;padding:7px 14px 7px 36px;border-radius:8px;
  border:1px solid #222340;
  background:#181930 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' fill='%23666' viewBox='0 0 16 16'%3E%3Cpath d='M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.85-3.85zm-5.242.656a5 5 0 1 1 0-10 5 5 0 0 1 0 10z'/%3E%3C/svg%3E") 12px center no-repeat;
  color:#e0e0e0;font-size:13px;width:240px;outline:none;transition:border-color .2s,width .3s}
#search-box:focus{border-color:#64b5f680;width:300px}
#search-box::placeholder{color:#444460}
.toolbar-btn{padding:5px 13px;border-radius:7px;border:1px solid #222340;
  background:#181930;color:#8888aa;font-size:12px;cursor:pointer;
  transition:all .2s;white-space:nowrap}
.toolbar-btn:hover{background:#222250;border-color:#444470;color:#bbb}
.toolbar-btn.active{background:#1a2a4a;border-color:#4488bb;color:#66aaee}
.toolbar-sep{width:1px;height:24px;background:#222340}
#edge-limit{width:52px;padding:4px 6px;border-radius:6px;border:1px solid #222340;
  background:#181930;color:#8888aa;font-size:12px;text-align:center;outline:none}
#edge-limit:focus{border-color:#4488bb;color:#bbb}
#pkg-level,#flow-depth{padding:4px 6px;border-radius:6px;border:1px solid #222340;
  background:#181930;color:#8888aa;font-size:12px;outline:none;cursor:pointer}
#pkg-level:focus,#flow-depth:focus{border-color:#4488bb;color:#bbb}
#pkg-level option,#flow-depth option{background:#181930;color:#8888aa}

#canvas-container{position:fixed;top:50px;left:0;right:0;bottom:0;
  overflow:hidden;cursor:grab;
  background:radial-gradient(circle at 20% 30%,rgba(40,50,80,.15) 0%,transparent 50%),
    radial-gradient(circle at 80% 70%,rgba(60,30,70,.1) 0%,transparent 50%),#0d0e1a}
#canvas-container.grabbing{cursor:grabbing}
#canvas{position:absolute;transform-origin:0 0}

/* SVG layer */
#edge-svg{position:absolute;top:0;left:0;overflow:visible;z-index:1;pointer-events:none}
#edge-svg path.e{pointer-events:none}
#edge-svg path.e-hit{pointer-events:stroke;cursor:pointer}
#edge-svg.on-top{z-index:50}

/* Blocks above SVG by default, but below when edges are on-top */
.module-block,.class-block,.func-block,.method-block{position:relative;z-index:2}

/* When edges on-top, push blocks below */
body.edges-on-top .package-block,
body.edges-on-top .module-block,
body.edges-on-top .class-block,
body.edges-on-top .func-block,
body.edges-on-top .method-block{z-index:1}
body.edges-on-top #edge-svg{z-index:50}

.package-block{position:absolute;
  background:linear-gradient(160deg,#1a1810,#1c1a12);
  border:1.5px solid #5a4a2a40;border-radius:14px;min-width:200px;overflow:hidden;
  box-shadow:0 8px 32px rgba(0,0,0,.35);transition:box-shadow .3s}
.package-block:hover{box-shadow:0 8px 40px rgba(60,50,20,.35),0 0 0 1px rgba(90,74,42,.2)}
.package-header{background:linear-gradient(135deg,#6a5a2a,#5a4a22);
  padding:10px 16px;border-radius:12px 12px 0 0;font-size:16px;font-weight:700;
  color:#e8d8a0;letter-spacing:.4px;user-select:none;display:flex;align-items:center;
  justify-content:center;gap:8px;cursor:pointer;transition:background .2s}
.package-header:hover{background:linear-gradient(135deg,#7a6a3a,#6a5a2a)}
.package-header .pi{width:20px;height:20px;border-radius:4px;background:rgba(255,255,255,.12);
  display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0}

.module-block{position:absolute;
  background:linear-gradient(160deg,#181930,#151628);
  border:1.5px solid #2a2b5540;border-radius:14px;min-width:180px;
  box-shadow:0 8px 32px rgba(0,0,0,.35);transition:box-shadow .3s}
.module-block:hover{box-shadow:0 8px 40px rgba(40,40,90,.35),0 0 0 1px rgba(60,60,120,.2)}
.module-header{background:linear-gradient(135deg,#3e4280,#33366a);
  padding:10px 16px;border-radius:12px 12px 0 0;font-size:13px;font-weight:700;
  color:#c8c8f0;letter-spacing:.4px;user-select:none;display:flex;align-items:center;gap:8px}
.module-header .mi{width:20px;height:20px;border-radius:4px;background:rgba(255,255,255,.12);
  display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0}
.module-body{padding:8px 8px 10px}

.class-block{background:linear-gradient(160deg,#162030,#142535);
  border:1px solid #1e557540;border-radius:10px;margin:7px 0;overflow:hidden;
  transition:border-color .2s,box-shadow .2s}
.class-block:hover{border-color:#2a8aaa;box-shadow:0 4px 16px rgba(30,85,117,.15)}
.class-header{background:linear-gradient(135deg,#1e6a8a,#1a5a78);
  padding:7px 13px;font-size:12.5px;font-weight:600;color:#a0d8f0;cursor:pointer;
  display:flex;align-items:center;gap:6px;transition:background .2s}
.class-header:hover{background:linear-gradient(135deg,#2878a0,#226a8a)}
.class-header .ci{width:17px;height:17px;border-radius:3px;background:rgba(255,255,255,.12);
  display:flex;align-items:center;justify-content:center;font-size:10px;flex-shrink:0}
.class-bases{font-size:11px;color:#5a98b8;font-weight:400}
.class-params{padding:4px 13px 6px;font-size:10.5px;color:#5a90aa;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;line-height:1.5;
  border-bottom:1px solid #1e557525;word-break:break-all}
.class-params .pl{color:#3a7a9a;font-weight:600}
.class-body{padding:4px 5px 6px}

.func-block{background:linear-gradient(160deg,#142014,#122518);
  border:1px solid #2a6a2a40;border-radius:7px;margin:4px 2px;cursor:pointer;
  transition:border-color .2s,box-shadow .2s,transform .15s}
.func-block:hover{border-color:#3a9a3a;box-shadow:0 2px 10px rgba(42,106,42,.15);transform:translateY(-1px)}
.func-header{padding:5px 11px;font-size:12px;font-weight:600;color:#80d880;
  display:flex;align-items:center;gap:5px}
.func-header .fi{width:15px;height:15px;border-radius:3px;background:rgba(60,160,60,.12);
  display:flex;align-items:center;justify-content:center;font-size:9px;color:#50a050;
  flex-shrink:0;font-weight:700}
.func-signature{padding:1px 11px 5px 31px;font-size:10px;color:#508a50;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;line-height:1.4;word-break:break-all}

.method-block{background:linear-gradient(160deg,#1e1420,#201828);
  border:1px solid #6a2a6a40;border-radius:7px;margin:3px 2px;cursor:pointer;
  transition:border-color .2s,box-shadow .2s,transform .15s}
.method-block:hover{border-color:#9a3a9a;box-shadow:0 2px 10px rgba(106,42,106,.15);transform:translateY(-1px)}
.method-header{padding:4px 11px;font-size:12px;font-weight:600;color:#c888c8;
  display:flex;align-items:center;gap:5px}
.method-header .fi{width:15px;height:15px;border-radius:3px;background:rgba(150,60,150,.12);
  display:flex;align-items:center;justify-content:center;font-size:9px;color:#a050a0;
  flex-shrink:0;font-weight:700}
.method-signature{padding:1px 11px 4px 31px;font-size:10px;color:#7a507a;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;line-height:1.4;word-break:break-all}

/* Source panel */
#source-panel{position:fixed;top:50px;right:-760px;width:720px;bottom:0;
  background:#0e0f22;border-left:1px solid #222340;z-index:90;
  transition:right .35s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;
  box-shadow:-8px 0 32px rgba(0,0,0,.5)}
#source-panel.open{right:0}
#sp-resize{position:absolute;top:0;left:-4px;width:8px;bottom:0;cursor:ew-resize;z-index:2}
#sp-resize::before{content:'';position:absolute;top:50%;left:3px;width:2px;height:32px;
  margin-top:-16px;background:#44446080;border-radius:1px}
#sp-hdr{padding:12px 16px;background:#14152a;border-bottom:1px solid #222340;
  display:flex;align-items:center;justify-content:space-between;gap:12px}
#sp-title{font-size:12px;font-weight:600;color:#5599cc;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;word-break:break-all}
#sp-close{background:none;border:1px solid #222340;color:#666;font-size:13px;
  cursor:pointer;padding:3px 10px;border-radius:5px;transition:all .2s}
#sp-close:hover{background:#222340;color:#ddd}
#sp-code{flex:1;overflow:auto;padding:14px 16px;
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;
  font-size:12px;line-height:1.7;white-space:pre;color:#a8a8c8;tab-size:4}
#sp-code .sk{color:#c586c0}#sp-code .sb{color:#4ec9b0}
#sp-code .ss{color:#ce9178}#sp-code .sc{color:#6a9955;font-style:italic}
#sp-code .sn{color:#b5cea8}#sp-code .sd{color:#dcdcaa}
#sp-code .sl{color:#4fc1ff}#sp-code .so{color:#d4d4d4}
#sp-code .sp{color:#808080}#sp-code .sf{color:#dcdcaa}
#sp-code .st{color:#4ec9b0}

/* Navigation minimap */
#minimap{position:fixed;bottom:16px;right:16px;width:200px;height:140px;
  background:#0e0f22e0;border:1px solid #222340;border-radius:10px;
  z-index:100;overflow:hidden;backdrop-filter:blur(8px);
  box-shadow:0 4px 20px rgba(0,0,0,.4);min-width:120px;min-height:80px}
#minimap canvas{width:100%;height:100%}
#mm-viewport{position:absolute;border:1.5px solid #64b5f680;border-radius:2px;
  pointer-events:none;background:rgba(100,181,246,.08)}
#mm-resize{position:absolute;top:0;left:0;width:26px;height:26px;cursor:nw-resize;z-index:8}
#mm-resize::before{content:'';position:absolute;top:6px;left:6px;width:9px;height:9px;
  border-left:2px solid #555570;border-top:2px solid #555570;opacity:.7}

/* Call-stack node list overlay (toggled from the minimap) */
#mm-stack-toggle{position:absolute;top:3px;right:3px;width:18px;height:18px;border-radius:5px;
  background:#222340cc;border:1px solid #444470;color:#aab0d0;font-size:11px;
  display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:6;
  line-height:1;user-select:none;transition:background .15s,color .15s}
#mm-stack-toggle:hover{background:#2a2a55;color:#fff}
#mm-stack-toggle.active{background:#1a2a4a;border-color:#4488bb;color:#66aaee}
#minimap.stack-open #mm-canvas,#minimap.stack-open #mm-viewport,
#minimap.stack-open #mm-stack-toggle{display:none}
#mm-stack{position:absolute;inset:0;display:none;flex-direction:column;
  background:#0e0f22f0;border-radius:10px;overflow:hidden;z-index:5}
#minimap.stack-open #mm-stack{display:flex}
#mm-stack-hdr{display:flex;align-items:center;justify-content:space-between;gap:6px;
  padding:6px 8px;font-size:10px;color:#8888aa;text-transform:uppercase;letter-spacing:1px;
  border-bottom:1px solid #222340;font-weight:600;flex-shrink:0}
#mm-stack-close{background:none;border:1px solid #222340;color:#888;font-size:13px;
  cursor:pointer;width:18px;height:18px;border-radius:5px;line-height:1;
  display:flex;align-items:center;justify-content:center;transition:all .2s}
#mm-stack-close:hover{background:#222340;color:#ddd}
#mm-stack-ctl{display:flex;align-items:center;gap:8px;padding:7px 8px;font-size:11px;
  color:#8888aa;border-bottom:1px solid #222340;flex-shrink:0}
#mm-stack-ctl b{color:#ffd080;font-weight:700;min-width:14px;text-align:center}
#mm-depth{flex:1;accent-color:#f5a623;cursor:pointer}
#mm-stack-list{flex:1;overflow-y:auto;padding:3px 0}
#mm-stack-list .ms-row{display:flex;align-items:center;gap:6px;padding:4px 8px;font-size:11px;
  color:#b0b0d0;cursor:pointer;font-family:'Cascadia Code','Fira Code',monospace;
  border-bottom:1px solid #14152a;word-break:break-all;line-height:1.3}
#mm-stack-list .ms-row:hover{background:#181930;color:#fff}
#mm-stack-list .ms-step{color:#f5a623;font-weight:700;flex-shrink:0;min-width:30px}
#mm-stack-list .ms-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#mm-stack-list .ms-d{color:#555570;flex-shrink:0;font-weight:400}
#mm-stack-list .ms-loop{color:#ffc070;background:#c97a0030;border:1px solid #c97a0060;
  border-radius:6px;padding:0 4px;font-size:9px;flex-shrink:0}
#mm-stack-list .ms-empty{padding:14px;font-size:11px;color:#555570;text-align:center}
#mm-stack-list .ms-more{padding:6px 8px;font-size:10px;color:#555570;text-align:center;
  font-style:italic}

/* Connection dots on block borders */
.conn-dot{position:absolute;width:8px;height:8px;border-radius:50%;z-index:200;
  cursor:pointer;transition:transform .15s,box-shadow .15s;pointer-events:auto}
.conn-dot.call{background:#5b9bd5;border:1.5px solid #3a7ab5;box-shadow:0 0 4px rgba(91,155,213,.3)}
.conn-dot.inherit{background:#ff6b6b;border:1.5px solid #cc5555;box-shadow:0 0 4px rgba(255,107,107,.3)}
.conn-dot:hover{transform:scale(1.6);box-shadow:0 0 8px rgba(100,181,246,.5)!important}
.conn-dot::after{content:'';position:absolute;top:-6px;left:-6px;width:20px;height:20px;border-radius:50%}
.package-block.search-hit{box-shadow:0 0 0 3px #ffd54f,0 8px 32px rgba(255,213,79,.2)!important}
.module-block.search-hit{box-shadow:0 0 0 3px #ffd54f,0 8px 32px rgba(255,213,79,.2)!important}
.class-block.search-hit{border-color:#ffd54f!important;box-shadow:0 0 0 2px #ffd54f60!important}
.func-block.search-hit,.method-block.search-hit{border-color:#ffd54f!important;box-shadow:0 0 0 2px #ffd54f60!important}
.block-hl{box-shadow:0 0 0 2px rgba(100,181,246,.5),0 0 16px rgba(100,181,246,.15)!important}

/* Per-block connection list button + dropdown */
.nav-btn{position:absolute;top:3px;right:3px;width:16px;height:16px;border-radius:4px;
  background:#222340cc;border:1px solid #444470;color:#aab0d0;font-size:10px;
  display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:6;
  opacity:0;pointer-events:none;transition:opacity .15s,background .15s;line-height:1;
  user-select:none}
.nav-btn:hover{background:#2a2a55;color:#fff}
[data-id]:hover .nav-btn{opacity:1;pointer-events:auto}
#nav-menu{position:fixed;z-index:300;background:#12132af0;border:1px solid #333360;
  border-radius:8px;box-shadow:0 8px 32px rgba(0,0,0,.6);display:none;min-width:200px;
  max-width:340px;backdrop-filter:blur(8px);overflow:hidden}
#nav-menu.open{display:block}
#nav-menu .nm-hdr{padding:7px 10px;font-size:11px;color:#8888aa;border-bottom:1px solid #222340;
  font-family:'Cascadia Code','Fira Code',monospace;word-break:break-all}
#nav-menu .nm-list{max-height:300px;overflow-y:auto}
#nav-menu .nm-item{padding:6px 12px;font-size:12px;color:#c0c0e0;cursor:pointer;display:flex;
  align-items:center;gap:8px;font-family:'Cascadia Code','Fira Code',monospace;word-break:break-all}
#nav-menu .nm-item:hover{background:#222250;color:#fff}
#nav-menu .nm-dir{width:12px;flex-shrink:0;text-align:center;font-weight:700}
#nav-menu .nm-dir.out{color:#5b9bd5}
#nav-menu .nm-dir.in{color:#c8a050}
#nav-menu .nm-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
#nav-menu .nm-dot.call{background:#5b9bd5}
#nav-menu .nm-dot.inherit{background:#ff6b6b}
#nav-menu .nm-empty{padding:12px;font-size:11px;color:#666;text-align:center}

/* Legend */
#legend{position:fixed;bottom:16px;left:16px;background:#12132af0;border:1px solid #222340;
  border-radius:10px;padding:12px 16px;z-index:100;font-size:11px;backdrop-filter:blur(8px)}
#legend h3{font-size:10px;margin-bottom:8px;color:#555570;font-weight:600;
  text-transform:uppercase;letter-spacing:1px}
.legend-row{display:flex;align-items:center;gap:8px;margin:4px 0;color:#8888aa}
.legend-swatch{width:14px;height:10px;border-radius:2px;flex-shrink:0}
.legend-line{width:24px;height:2px;flex-shrink:0;border-radius:1px}

#stats{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);
  background:#12132af0;border:1px solid #222340;border-radius:8px;padding:6px 18px;
  font-size:11px;color:#555570;z-index:100;display:flex;gap:16px;backdrop-filter:blur(8px)}
.sv{color:#5599cc;font-weight:600}
#zoom{position:fixed;top:60px;right:16px;font-size:11px;color:#444460;z-index:100;
  font-family:'Cascadia Code','Fira Code',monospace}
#tip{position:fixed;background:#1a1b30f0;border:1px solid #333360;border-radius:7px;
  padding:7px 12px;font-size:11px;color:#b0b0d0;pointer-events:none;z-index:200;
  max-width:380px;display:none;box-shadow:0 4px 16px rgba(0,0,0,.5);
  font-family:'Cascadia Code','Fira Code','Consolas',monospace;backdrop-filter:blur(8px)}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:#0e0f22}
::-webkit-scrollbar-thumb{background:#333360;border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#4a4a80}

/* ---- tracearc additions: runtime flow ---- */
#edge-svg path.ef{stroke:#f5a623;stroke-width:2;fill:none;opacity:.85}
#edge-svg path.ef-hl{stroke:#ffd080;stroke-width:3;opacity:1}
.flow-label{pointer-events:none}
.flow-label rect{fill:#1a1304;stroke:#f5a623;stroke-width:1}
.flow-label text{fill:#ffd080;font:700 10px 'Cascadia Code','Fira Code',monospace;text-anchor:middle;dominant-baseline:central}
.flow-dot{position:absolute;width:7px;height:7px;border-radius:50%;background:#f5a623;
  border:1px solid #c97a00;box-shadow:0 0 5px rgba(245,166,35,.4);z-index:200;
  pointer-events:auto;cursor:pointer}
.flow-dot:hover{transform:scale(1.6)}
.flow-dot.temp{opacity:.5}

/* Runtime badges on blocks */
.rt-badges{display:inline-flex;align-items:center;gap:4px;margin-left:auto;
  font:600 10px 'Cascadia Code','Fira Code',monospace}
.count-badge{background:#f5a62330;color:#ffd080;border:1px solid #f5a62360;
  border-radius:8px;padding:0 5px;white-space:nowrap}
.dur-badge{color:#7a7a9a;font-weight:400;white-space:nowrap}
.loop-badge{background:#c97a0030;color:#ffc070;border:1px solid #c97a0060;
  border-radius:8px;padding:0 5px;white-space:nowrap}
.entry-badge,.end-badge{font:700 9px 'Segoe UI',sans-serif;border-radius:4px;
  padding:1px 5px;white-space:nowrap;margin-left:5px}
.entry-badge{background:#2a6a2a;color:#9fe8a0;border:1px solid #3a9a3a}
.end-badge{background:#6a2a2a;color:#ff9a9a;border:1px solid #9a3a3a}

/* Entry / end block rings */
.func-block.block-entry,.method-block.block-entry{box-shadow:0 0 0 2px #3a9a3a,0 0 14px rgba(58,154,58,.3)!important}
.func-block.block-end,.method-block.block-end{box-shadow:0 0 0 2px #9a3a3a,0 0 14px rgba(154,58,58,.3)!important}
.class-block.block-entry{border-color:#3a9a3a!important;box-shadow:0 0 0 2px #3a9a3a60!important}
.class-block.block-end{border-color:#9a3a3a!important;box-shadow:0 0 0 2px #9a3a3a60!important}

/* Uncalled (static-only) blocks are dimmed */
.uncalled{opacity:.28;filter:grayscale(.5)}
.uncalled:hover{opacity:.55}
body.hide-uncalled .uncalled{display:none!important}

/* External summary panel (off by default) */
#external-panel{position:fixed;top:60px;right:16px;width:300px;max-height:60vh;
  overflow:auto;background:#0e0f22f0;border:1px solid #333360;border-radius:10px;
  z-index:95;display:none;backdrop-filter:blur(8px);box-shadow:0 8px 32px rgba(0,0,0,.5);
  padding:10px 12px}
#external-panel.open{display:block}
#external-panel h4{font-size:10px;color:#555570;text-transform:uppercase;letter-spacing:1px;
  margin-bottom:8px;font-weight:600}
.ext-item{display:flex;align-items:center;gap:7px;padding:5px 7px;border-radius:6px;
  font:11px 'Cascadia Code','Fira Code',monospace;color:#b0b0d0;cursor:default;
  border-bottom:1px solid #1a1b30}
.ext-item:hover{background:#181930}
.ext-item .ext-dot{width:7px;height:7px;border-radius:50%;background:#888;flex-shrink:0}
.ext-item .ext-pkg{color:#666680;font-size:9px;margin-left:auto;white-space:nowrap}
.ext-item .ext-cnt{color:#ffd080}
body.hide-entryend .entry-badge,body.hide-entryend .end-badge{display:none}
body.hide-entryend .block-entry,body.hide-entryend .block-end{box-shadow:none!important}
"""

    def _get_html_body(self, nodes_json: str, edges_json: str,
                       inheritance_json: str, source_json: str,
                       flow_json: str) -> str:
        # Escape ``<`` so embedded source code containing ``</script>`` cannot
        # break out of the JSON <script> blob (JSON allows <).
        def _esc(s):
            return s.replace("<", "\\u003c")
        nodes_json = _esc(nodes_json)
        edges_json = _esc(edges_json)
        inheritance_json = _esc(inheritance_json)
        source_json = _esc(source_json)
        flow_json = _esc(flow_json)
        return f"""
<div id="toolbar">
    <span class="logo">tracearc</span>
    <span class="project-name">{html.escape(self.title)}</span>
    <input type="text" id="search-box" placeholder="Search modules, classes, functions..." />
    <div class="toolbar-sep"></div>
    <button class="toolbar-btn" id="btn-fit" title="Fit to screen (F)">Fit</button>
    <button class="toolbar-btn active" id="btn-flow" title="Toggle numbered execution-flow arrows">Flow</button>
    <select id="flow-depth" title="Show flow arrows only for call-stack depths <= this value">
      <option value="all" selected>All depths</option>
    </select>
    <button class="toolbar-btn active" id="btn-entryend" title="Show program entry / end markers">Entry/End</button>
    <button class="toolbar-btn" id="btn-external" title="Show external (out-of-project) call summaries">External</button>
    <button class="toolbar-btn" id="btn-uncalled" title="Hide static-only (uncalled-at-runtime) blocks">Hide uncalled</button>
    <div class="toolbar-sep"></div>
    <button class="toolbar-btn" id="btn-edges" title="Toggle static edges (off by default)">Edges</button>
    <button class="toolbar-btn" id="btn-calls" title="Toggle static call edges">Calls</button>
    <button class="toolbar-btn" id="btn-inherit" title="Toggle inheritance edges">Inherit</button>
    <select id="pkg-level" title="Show package edges for this depth and below">
      <option value="none" selected>None</option>
    </select>
    <button class="toolbar-btn active" id="btn-top" title="Edges on top layer">Edges Top</button>
    <button class="toolbar-btn" id="btn-collapse" title="Collapse all">Collapse</button>
    <div class="toolbar-sep"></div>
    <span style="font-size:11px;color:#555570">Max edges &amp; flow</span>
    <input type="number" id="edge-limit" value="100" min="0" max="9999" title="Max static edges AND flow arrows to display (0=all; over the limit both are randomly sampled)" />
</div>
<div id="canvas-container">
    <div id="canvas">
        <svg id="edge-svg" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
</div>
<div id="source-panel">
    <div id="sp-resize"></div>
    <div id="sp-hdr"><span id="sp-title">Source</span><button id="sp-close">Close</button></div>
    <pre id="sp-code"></pre>
</div>
<div id="minimap">
    <div id="mm-resize"></div>
    <canvas id="mm-canvas"></canvas>
    <div id="mm-viewport"></div>
    <button id="mm-stack-toggle" title="Toggle call-stack node list">&#9776;</button>
    <div id="mm-stack">
        <div id="mm-stack-hdr"><span>Call stack</span><button id="mm-stack-close">&times;</button></div>
        <div id="mm-stack-ctl">
            <span>Depth &le; <b id="mm-depth-val">1</b></span>
            <input type="range" id="mm-depth" min="1" max="1" value="1" />
        </div>
        <div id="mm-stack-list"></div>
    </div>
</div>
<div id="external-panel">
    <h4>External calls <span id="ext-count" style="color:#666680"></span></h4>
    <div id="ext-list"></div>
</div>
<div id="legend">
    <h3>Legend</h3>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#6a5a2a,#5a4a22)"></div>Pkg L0</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#2a6a7a,#1e5a6a)"></div>Pkg L1</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#7a4a7a,#6a3a6a)"></div>Pkg L2</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#6a3a6a,#5a2a5a)"></div>Pkg L3</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#3a7a6a,#2a6a5a)"></div>Pkg L4</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#7a6a3a,#6a5a2a)"></div>Pkg L5</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#3e4280,#33366a)"></div>Module</div>
    <div class="legend-row"><div class="legend-swatch" style="background:linear-gradient(135deg,#1e6a8a,#1a5a78)"></div>Class</div>
    <div class="legend-row"><div class="legend-swatch" style="background:#142014;border:1px solid #2a6a2a"></div>Function</div>
    <div class="legend-row"><div class="legend-swatch" style="background:#1e1420;border:1px solid #6a2a6a"></div>Method</div>
    <div class="legend-row"><div class="legend-line" style="background:#5b9bd5"></div>Call (static)</div>
    <div class="legend-row"><div class="legend-line" style="background:#ff6b6b;border-top:2px dashed #ff6b6b;height:0"></div>Inherit (static)</div>
    <div class="legend-row"><div class="legend-line" style="background:#f5a623"></div>Flow (by depth)</div>
    <div class="legend-row" id="depth-legend-row" style="display:none;flex-wrap:wrap"><span style="font-size:10px;color:#555570;min-width:42px">Depth</span><span id="depth-chips" style="display:flex;gap:5px;flex-wrap:wrap;align-items:center"></span></div>
    <div class="legend-row"><div class="legend-swatch" style="background:#3a9a3a"></div>Entry ▶</div>
    <div class="legend-row"><div class="legend-swatch" style="background:#9a3a3a"></div>End ■</div>
    <div class="legend-row"><div class="legend-swatch" style="background:#888;border-radius:50%"></div>External</div>
</div>
<div id="stats">
    <span>Packages: <span class="sv" id="s-pkg">0</span></span>
    <span>Modules: <span class="sv" id="s-mod">0</span></span>
    <span>Classes: <span class="sv" id="s-cls">0</span></span>
    <span>Functions: <span class="sv" id="s-fn">0</span></span>
    <span>Edges: <span class="sv" id="s-edge">0</span></span>
    <span>Flow steps: <span class="sv" id="s-steps" style="color:#f5a623">0</span></span>
    <span>Entry: <span class="sv" id="s-entry" style="color:#3a9a3a">—</span></span>
    <span>End: <span class="sv" id="s-end" style="color:#9a3a3a">—</span></span>
</div>
<div id="zoom">100%</div>
<div id="tip"></div>
<div id="nav-menu"></div>
<script id="d-nodes" type="application/json">{nodes_json}</script>
<script id="d-edges" type="application/json">{edges_json}</script>
<script id="d-inherit" type="application/json">{inheritance_json}</script>
<script id="d-source" type="application/json">{source_json}</script>
<script id="d-flow" type="application/json">{flow_json}</script>
"""

    def _get_javascript(self) -> str:
        return r"""
(function(){
"use strict";

var ND=JSON.parse(document.getElementById('d-nodes').textContent);
var ED=JSON.parse(document.getElementById('d-edges').textContent);
var ID=JSON.parse(document.getElementById('d-inherit').textContent);
var SD=JSON.parse(document.getElementById('d-source').textContent);
var FD=JSON.parse(document.getElementById('d-flow').textContent);

var scale=1,panX=40,panY=40;
var dragging=false,dsx=0,dsy=0,psx=0,psy=0;
// Static call/inherit edges are OFF by default; the numbered execution flow is ON.
var showEdges=false,showCalls=false,showInherit=false,allCollapsed=false,edgesOnTop=true;
var showFlow=true,showEntryEnd=true,showExternal=false,hideUncalled=false;
// 'all' = show every flow arrow; a number d = show only arrows whose destination
// step is at call-stack depth <= d (cumulative, like the minimap slider).
var flowDepthFilter='all';
var searchTerm="";
var lockedId=null;

// First loop-repeat count seen for each block id (for the ↻×N badge).
var loopByBlock={};(FD.steps||[]).forEach(function(s){if(s.loop_count&&loopByBlock[s.id]===undefined)loopByBlock[s.id]=s.loop_count;});

var ctnr=document.getElementById('canvas-container');
var cvs=document.getElementById('canvas');
var svg=document.getElementById('edge-svg');
var tip=document.getElementById('tip');
var zoomEl=document.getElementById('zoom');
var EM={};var pkgDepthMap=new Map();var maxPkgDepth=-1;

// Precompute the set of block ids that participate in any edge (including
// ancestor package/module/class blocks of leaf endpoints), so we only render
// the connection button on blocks that actually have connections.
var connectedBlockIds=new Set();
(function(){
  function note(ep){var parts=ep.split('.');for(var i=parts.length;i>=1;i--)connectedBlockIds.add(parts.slice(0,i).join('.'));}
  ED.forEach(function(e){note(e.source);note(e.target);});
  ID.forEach(function(e){note(e.source);note(e.target);});
})();

var posCache=null,posDirty=true;

var edgeLimitInput=document.getElementById('edge-limit');
var maxEdges=parseInt(edgeLimitInput.value)||100;
edgeLimitInput.addEventListener('change',function(){maxEdges=parseInt(this.value)||0;scheduleDrawEdges();});

var nPkg=0,nMod=0,nCls=0,nFn=0;
function countStats(node){
    if(node.type==='package'){nPkg++;node.children.forEach(countStats);}
    else if(node.type==='module'){nMod++;node.children.forEach(function(c){if(c.type==='class')nCls++;else nFn++;});}
}
if(ND.type==='package'){countStats(ND);}else{ND.forEach(function(m){nMod++;m.children.forEach(function(c){if(c.type==='class')nCls++;else nFn++;})});}
document.getElementById('s-pkg').textContent=nPkg;
document.getElementById('s-mod').textContent=nMod;
document.getElementById('s-cls').textContent=nCls;
document.getElementById('s-fn').textContent=nFn;
document.getElementById('s-edge').textContent=ED.length+ID.length;
function shortName(id){if(!id)return '—';var p=String(id).split('.');return p.slice(-2).join('.');}
function fmtDur(s){s=s||0;if(s<=0)return '';if(s<1e-3)return Math.round(s*1e6)+'µs';if(s<1)return Math.round(s*1e3)+'ms';return s.toFixed(2)+'s';}
var flowSteps=FD.steps||[],flowEdges=FD.edges||[];
// step number -> call-stack depth (a flow edge's depth = its destination step's depth).
var stepDepth={};flowSteps.forEach(function(s){stepDepth[s.step]=s.depth;});
// Deepest call-stack depth present in the trace (drives the toolbar dropdown and
// the minimap slider range). 0 when there is no trace (pure static view).
var maxFlowDepth=0;flowSteps.forEach(function(s){if(s.depth>maxFlowDepth)maxFlowDepth=s.depth;});
// Partial Fisher-Yates shuffle: pick n random items from arr (returns a new
// array). Shared by static-edge and flow-arrow limiting so both "randomly
// select" identically.
function randomSample(arr,n){var a=arr.slice();n=Math.min(n,a.length);
  for(var i=a.length-1;i>0&&i>=a.length-n;i--){var j=Math.floor(Math.random()*(i+1));var t=a[i];a[i]=a[j];a[j]=t;}
  return a.slice(a.length-n);}
// Call-stack depth -> color, shared by the minimap call-stack nodes and the
// flow arrows so a given depth reads as the same color in both. Warm -> cool
// spectrum, cycling for very deep traces.
var DEPTH_COLORS=['#ff6b6b','#f5a623','#ffd54f','#66d9a6','#4fc1ff','#9c7cff','#ff7ce0','#5be0c0'];
function depthColorIdx(d){if(!d||d<1)d=1;return (d-1)%DEPTH_COLORS.length;}
function depthColor(d){return DEPTH_COLORS[depthColorIdx(d)];}
document.getElementById('s-steps').textContent=flowSteps.length;
document.getElementById('s-entry').textContent=shortName(FD.entry);
document.getElementById('s-end').textContent=shortName(FD.end)+(FD.crashed?' (crashed)':'');

function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}

// External (out-of-project) call summaries, shown in a side panel when toggled.
function renderExternal(){
    var list=document.getElementById('ext-list');list.innerHTML='';
    var ext=FD.external||[];
    document.getElementById('ext-count').textContent=ext.length?('('+ext.length+')'):'';
    if(!ext.length){list.innerHTML='<div style="color:#666;padding:6px">No external calls recorded</div>';return;}
    ext.forEach(function(e){
        var d=document.createElement('div');d.className='ext-item';
        d.innerHTML='<span class="ext-dot"></span><span>'+esc(e.label||e.id)+'</span>'+
            (e.count>1?'<span class="ext-cnt">×'+e.count+'</span>':'')+
            '<span class="ext-pkg">'+esc(e.pkg||'external')+'</span>';
        d.title=(e.label||'')+(e.pkg?('  ['+e.pkg+']'):'')+(e.duration?('  '+fmtDur(e.duration)):'');
        list.appendChild(d);
    });
}

// Attach runtime badges (count / duration / loop) and entry/end markers; dim
// static-only (uncalled) blocks. node carries {hit,count,duration,is_entry,is_end}.
function applyRuntime(el,node){
  if(!node.hit){el.classList.add('uncalled');return;}
  var hdr=el.querySelector('.func-header,.method-header,.class-header');
  var b=[];
  if(node.is_entry)b.push('<span class="entry-badge">▶</span>');
  if(node.is_end)b.push('<span class="end-badge">■</span>');
  if(loopByBlock[node.id])b.push('<span class="loop-badge">↻×'+loopByBlock[node.id]+'</span>');
  if(node.count>1)b.push('<span class="count-badge">×'+node.count+'</span>');
  if(node.duration>0)b.push('<span class="dur-badge">'+fmtDur(node.duration)+'</span>');
  if(b.length&&hdr){var sp=document.createElement('span');sp.className='rt-badges';sp.innerHTML=b.join('');hdr.appendChild(sp);}
  if(node.is_entry)el.classList.add('block-entry');
  if(node.is_end)el.classList.add('block-end');
}

// ========== Build DOM — FLAT: all blocks direct children of #canvas ==========
function buildAll(){
    if(ND.type==='package'){buildPackageFlat(ND,0);}
    else{ND.forEach(function(m){var el=buildModule(m);cvs.appendChild(el);EM[m.id]=el;});}
    requestAnimationFrame(function(){
        layoutTree();
        requestAnimationFrame(function(){drawEdges();fitToScreen();updateMinimap();});
    });
}

function buildPackageFlat(pkg,depth){
    var el=document.createElement('div');el.className='package-block';el.dataset.id=pkg.id;
    // Depth-based color gradient
    var depthColors=[
        {bg:'#1a1810',border:'#5a4a2a',hdr1:'#6a5a2a',hdr2:'#5a4a22',hover1:'#7a6a3a',hover2:'#6a5a2a'},
        {bg:'#101a1e',border:'#2a5a6a',hdr1:'#2a6a7a',hdr2:'#1e5a6a',hover1:'#3a7a8a',hover2:'#2a6a7a'},
        {bg:'#18101a',border:'#6a3a6a',hdr1:'#7a4a7a',hdr2:'#6a3a6a',hover1:'#8a5a8a',hover2:'#7a4a7a'},
        {bg:'#1a1018',border:'#5a2a5a',hdr1:'#6a3a6a',hdr2:'#5a2a5a',hover1:'#7a4a7a',hover2:'#6a3a6a'},
        {bg:'#10181a',border:'#2a6a5a',hdr1:'#3a7a6a',hdr2:'#2a6a5a',hover1:'#4a8a7a',hover2:'#3a7a6a'},
        {bg:'#1a1510',border:'#6a5a2a',hdr1:'#7a6a3a',hdr2:'#6a5a2a',hover1:'#8a7a4a',hover2:'#7a6a3a'},
    ];
    var ci=depth%depthColors.length;
    var dc=depthColors[ci];
    el.style.background='linear-gradient(160deg,'+dc.bg+','+dc.bg+')';
    el.style.borderColor=dc.border+'60';
    var hdr=document.createElement('div');hdr.className='package-header';
    hdr.style.background='linear-gradient(135deg,'+dc.hdr1+','+dc.hdr2+')';
    hdr.innerHTML='<span class="pi">P</span>'+esc(pkg.label);hdr.title=pkg.full_name;
    hdr.addEventListener('click',function(ev){
        ev.stopPropagation();
        var collapsed=el.dataset.collapsed==='1';
        if(collapsed){el.dataset.collapsed='0';showDescendants(pkg);}
        else{el.dataset.collapsed='1';hideDescendants(pkg);}
        invalidateMeasure();
        setTimeout(function(){drawEdges();fitToScreen();updateMinimap();},60);
    });
    hdr.addEventListener('mouseenter',function(){hdr.style.background='linear-gradient(135deg,'+dc.hover1+','+dc.hover2+')';});
    hdr.addEventListener('mouseleave',function(){hdr.style.background='linear-gradient(135deg,'+dc.hdr1+','+dc.hdr2+')';});
    el.appendChild(hdr);
    addNavBtn(el,pkg.id);
    cvs.appendChild(el);EM[pkg.id]=el;pkgDepthMap.set(pkg.id,depth);if(depth>maxPkgDepth)maxPkgDepth=depth;
    pkg.children.forEach(function(ch){
        if(ch.type==='package')buildPackageFlat(ch,depth+1);
        else{var mel=buildModule(ch);cvs.appendChild(mel);EM[ch.id]=mel;}
    });
}

function hideDescendants(pkg){pkg.children.forEach(function(ch){var el=EM[ch.id];if(el)el.style.display='none';if(ch.type==='package')hideDescendants(ch);});}
function showDescendants(pkg){pkg.children.forEach(function(ch){var el=EM[ch.id];if(el)el.style.display='';if(ch.type==='package')showDescendants(ch);});}

function buildModule(mod){
    var el=document.createElement('div');el.className='module-block';el.dataset.id=mod.id;
    var hdr=document.createElement('div');hdr.className='module-header';
    hdr.innerHTML='<span class="mi">M</span>'+esc(mod.label);hdr.title=mod.full_name;
    el.appendChild(hdr);
    var body=document.createElement('div');body.className='module-body';
    mod.children.forEach(function(ch){body.appendChild(ch.type==='class'?buildClass(ch):buildFunc(ch));});
    el.appendChild(body);addNavBtn(el,mod.id);return el;
}
function buildClass(cls){
    var el=document.createElement('div');el.className='class-block';el.dataset.id=cls.id;
    var hdr=document.createElement('div');hdr.className='class-header';
    var h='<span class="ci">C</span>'+esc(cls.name);
    if(cls.bases&&cls.bases.length>0)h+=' <span class="class-bases">→ '+esc(cls.bases.join(', '))+'</span>';
    hdr.innerHTML=h;hdr.addEventListener('click',function(ev){ev.stopPropagation();openSource(cls);});
    el.appendChild(hdr);
    if(cls.init_params&&cls.init_params.length>0){var p=document.createElement('div');p.className='class-params';p.innerHTML='<span class="pl">init</span>('+cls.init_params.map(function(x){return esc(x);}).join(', ')+')';el.appendChild(p);}
    if(cls.children&&cls.children.length>0){var cb=document.createElement('div');cb.className='class-body';cls.children.forEach(function(m){cb.appendChild(buildMethod(m));});el.appendChild(cb);}
    applyRuntime(el,cls);addNavBtn(el,cls.id);return el;
}
function buildFunc(fn){
    var el=document.createElement('div');el.className='func-block';el.dataset.id=fn.id;
    var hdr=document.createElement('div');hdr.className='func-header';
    var h='<span class="fi">f</span>'+esc(fn.name);
    if(fn.return_type)h+=' <span style="color:#408040;font-size:11px">→ '+esc(fn.return_type)+'</span>';
    hdr.innerHTML=h;hdr.addEventListener('click',function(ev){ev.stopPropagation();openSource(fn);});
    el.appendChild(hdr);
    if(fn.params&&fn.params.length>0){var s=document.createElement('div');s.className='func-signature';s.textContent='('+fn.params.join(', ')+')';el.appendChild(s);}
    applyRuntime(el,fn);addNavBtn(el,fn.id);return el;
}
function buildMethod(m){
    var el=document.createElement('div');el.className='method-block';el.dataset.id=m.id;
    var hdr=document.createElement('div');hdr.className='method-header';
    var h='<span class="fi">f</span>'+esc(m.name);
    if(m.return_type)h+=' <span style="color:#704080;font-size:11px">→ '+esc(m.return_type)+'</span>';
    hdr.innerHTML=h;hdr.addEventListener('click',function(ev){ev.stopPropagation();openSource(m);});
    el.appendChild(hdr);
    if(m.params&&m.params.length>0){var s=document.createElement('div');s.className='method-signature';s.textContent='('+m.params.join(', ')+')';el.appendChild(s);}
    applyRuntime(el,m);addNavBtn(el,m.id);return el;
}

// ========== Per-block connection list (dropdown) ==========
function addNavBtn(el,id){
    if(!connectedBlockIds.has(id))return;
    var b=document.createElement('div');b.className='nav-btn';b.textContent='⇄';
    b.title='View connections';
    b.addEventListener('click',function(ev){ev.stopPropagation();openNavMenu(id,ev.clientX,ev.clientY);});
    el.appendChild(b);
}
function getConnections(blockId){
    // Collect connected edges: an edge is connected if either endpoint equals
    // blockId or sits under it (blockId + '.'). Internal edges (both sides under
    // blockId) are skipped. The "other" endpoint is what we can navigate to.
    var map={};
    function add(other,dir,type){if(!map[other])map[other]={dirs:{},types:{}};map[other].dirs[dir]=1;map[other].types[type]=1;}
    function handle(s,t,type){
        var sHere=s===blockId||s.startsWith(blockId+'.'),tHere=t===blockId||t.startsWith(blockId+'.');
        if(sHere&&tHere)return;
        if(sHere)add(t,'out',type);else if(tHere)add(s,'in',type);
    }
    ED.forEach(function(e){handle(e.source,e.target,'call');});
    ID.forEach(function(e){handle(e.source,e.target,'inherit');});
    return Object.keys(map).sort().map(function(k){return {other:k,dirs:map[k].dirs,types:map[k].types};});
}
var navMenu=document.getElementById('nav-menu');
function closeNavMenu(){navMenu.classList.remove('open');navMenu.innerHTML='';}
function openNavMenu(id,x,y){
    var conns=getConnections(id);
    navMenu.innerHTML='';
    var h=document.createElement('div');h.className='nm-hdr';
    h.textContent=id+'  ('+conns.length+')';navMenu.appendChild(h);
    var list=document.createElement('div');list.className='nm-list';
    if(conns.length===0){var em=document.createElement('div');em.className='nm-empty';em.textContent='No connections';list.appendChild(em);}
    conns.forEach(function(c){
        var it=document.createElement('div');it.className='nm-item';
        var both=c.dirs.out&&c.dirs.in;
        var dir=document.createElement('span');dir.className='nm-dir '+(c.dirs.out?'out':'in');
        dir.textContent=both?'⇄':(c.dirs.out?'→':'←');it.appendChild(dir);
        if(c.types.call){var d1=document.createElement('span');d1.className='nm-dot call';it.appendChild(d1);}
        if(c.types.inherit){var d2=document.createElement('span');d2.className='nm-dot inherit';it.appendChild(d2);}
        var lab=document.createElement('span');lab.textContent=c.other.split('.').slice(-2).join('.');lab.title=c.other;
        it.appendChild(lab);
        it.addEventListener('click',function(ev){ev.stopPropagation();navigateToBlock(c.other);closeNavMenu();});
        list.appendChild(it);
    });
    navMenu.appendChild(list);
    navMenu.style.left='0px';navMenu.style.top='0px';navMenu.classList.add('open');
    var mw=navMenu.offsetWidth,mh=navMenu.offsetHeight;
    var nx=Math.max(8,Math.min(x,window.innerWidth-mw-8));
    var ny=Math.max(8,Math.min(y,window.innerHeight-mh-8));
    navMenu.style.left=nx+'px';navMenu.style.top=ny+'px';
}
// Close the dropdown on outside mousedown (nav-btn clicks stopPropagation, so
// they re-open rather than close).
document.addEventListener('mousedown',function(e){if(navMenu.classList.contains('open')&&!navMenu.contains(e.target))closeNavMenu();});

// ========== Grid Layout — bottom-up, max 16 per row, generous gaps ==========
var MAX_PER_ROW=8;
var PKG_HDR_H=38;
var PKG_PAD=20;       // padding inside package around children
var LEAF_GAP=24;      // gap between leaf modules (same level)
var PKG_GAP_X=40;     // horizontal gap between sibling packages
var PKG_GAP_Y=40;     // vertical gap between sibling packages

function layoutTree(){
    if(ND.type==='package'){
        // Step 1: Measure natural sizes of leaf modules (walk the tree)
        var leafSizes={};
        function walkMeasure(n){
            if(n.type==='module'){
                var el=EM[n.id];
                if(el){var r=el.getBoundingClientRect();leafSizes[n.id]={w:r.width,h:r.height};}
                else{leafSizes[n.id]={w:300,h:200};}
            }else if(n.type==='package'){n.children.forEach(walkMeasure);}
        }
        walkMeasure(ND);

        // Step 2: Bottom-up compute sizes for all packages
        computeNodeSize(ND,leafSizes);

        // Step 3: Top-down assign positions (canvas-absolute)
        layoutNode(ND,0,0);

    }else{
        // Fallback flat
        var sizes={};
        ND.forEach(function(m){var el=EM[m.id];if(el){var r=el.getBoundingClientRect();sizes[m.id]={w:r.width,h:r.height};}});
        var GAP_X=120,GAP_Y=80;
        var sortedMods=ND.slice().sort(function(a,b){return a.name.localeCompare(b.name);});
        var totalArea=0;
        sortedMods.forEach(function(m){var s=sizes[m.id]||{w:300,h:200};totalArea+=(s.w+GAP_X)*(s.h+GAP_Y);});
        var targetW=Math.sqrt(totalArea*1.6);
        var curX=0,curY=0,rowH=0;
        sortedMods.forEach(function(m){
            var s=sizes[m.id]||{w:300,h:200};
            if(curX+s.w+GAP_X>targetW&&curX>0){curX=0;curY+=rowH+GAP_Y;rowH=0;}
            var el=EM[m.id];if(el){el.style.left=curX+'px';el.style.top=curY+'px';}
            curX+=s.w+GAP_X;rowH=Math.max(rowH,s.h);
        });
    }
}

function computeNodeSize(node,leafSizes){
    if(node.type==='module'){
        // Leaf: use measured size
        var s=leafSizes[node.id]||{w:300,h:200};
        node._size={w:s.w,h:s.h};
        return;
    }
    if(node.type==='package'){
        // First compute sizes of all children
        node.children.forEach(function(ch){computeNodeSize(ch,leafSizes);});

        // Determine gap for this level's children
        var hasAllModules=node.children.every(function(c){return c.type==='module';});
        var gapX=hasAllModules?LEAF_GAP:PKG_GAP_X;
        var gapY=hasAllModules?LEAF_GAP:PKG_GAP_Y;

        // Arrange children in rows of MAX_PER_ROW
        var rows=[];
        var curRow=[];
        var rowW=0;
        node.children.forEach(function(ch){
            curRow.push(ch);
            rowW+=ch._size.w+gapX;
            if(curRow.length>=MAX_PER_ROW){rows.push(curRow);curRow=[];rowW=0;}
        });
        if(curRow.length>0)rows.push(curRow);

        // Compute total width = max row width, total height = sum of row heights
        var totalW=0;
        rows.forEach(function(row){
            var w=gapX; // start with one gap margin
            row.forEach(function(ch){w+=ch._size.w+gapX;});
            if(w>totalW)totalW=w;
        });
        var totalH=gapY;
        rows.forEach(function(row){
            var maxH=0;
            row.forEach(function(ch){if(ch._size.h>maxH)maxH=ch._size.h;});
            totalH+=maxH+gapY;
        });

        // Package size = children area + header + padding on all sides
        node._size={
            w:totalW+2*PKG_PAD,
            h:totalH+PKG_HDR_H+2*PKG_PAD
        };
        // Store rows for layout phase
        node._rows=rows;
        node._childGapX=gapX;
        node._childGapY=gapY;
    }
}

function layoutNode(node,parentX,parentY){
    if(node.type==='module'){
        // Position the module block at canvas-absolute coords
        var el=EM[node.id];
        if(el){
            el.style.left=parentX+'px';
            el.style.top=parentY+'px';
            el.style.width=node._size.w+'px';
        }
        return;
    }
    if(node.type==='package'){
        // Position the package background block
        var el=EM[node.id];
        var pkgX=parentX;
        var pkgY=parentY;
        var pkgW=node._size.w;
        var pkgH=node._size.h;
        if(el){
            el.style.left=pkgX+'px';
            el.style.top=pkgY+'px';
            el.style.width=pkgW+'px';
            el.style.height=pkgH+'px';
        }

        // Layout children inside the package body
        var bodyX=pkgX+PKG_PAD;
        var bodyY=pkgY+PKG_HDR_H+PKG_PAD;
        var gapX=node._childGapX;
        var gapY=node._childGapY;
        var rows=node._rows||[];

        var curY=bodyY;
        rows.forEach(function(row){
            var curX=bodyX+gapX;
            var rowH=0;
            row.forEach(function(ch){
                layoutNode(ch,curX,curY);
                curX+=ch._size.w+gapX;
                if(ch._size.h>rowH)rowH=ch._size.h;
            });
            curY+=rowH+gapY;
        });
    }
}

// ========== Syntax Highlighting (Python) ==========
var KW='await|break|continue|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|and|as|pass|raise|return|try|while|with|yield|async|def|class'.split('|');
var BI='print|len|range|enumerate|zip|map|filter|sorted|reversed|iter|next|id|hash|type|isinstance|issubclass|super|getattr|setattr|delattr|hasattr|callable|property|staticmethod|classmethod|abs|all|any|bin|bool|bytearray|bytes|chr|complex|dict|dir|divmod|eval|exec|float|format|frozenset|hex|input|int|list|max|min|object|oct|open|ord|pow|repr|round|set|slice|str|sum|tuple|vars|__import__|Exception|BaseException|ValueError|TypeError|KeyError|IndexError|AttributeError|RuntimeError|StopIteration|GeneratorExit|KeyboardInterrupt|OverflowError|ZeroDivisionError|FileNotFoundError|ImportError|ModuleNotFoundError|NameError|UnboundLocalError|OSError|IOError|EOFError|MemoryError|RecursionError|NotImplementedError|AssertionError|ArithmeticError|LookupError|Warning|UserWarning|DeprecationWarning|FutureWarning|PendingDeprecationWarning|RuntimeWarning|SyntaxWarning|ResourceWarning|BufferWarning'.split('|');
var KW_SET=new Set(KW),BI_SET=new Set(BI);
var DQ3='"'+'""',SQ3="'"+"''";
var RE_TRI=new RegExp('(?:[rRbBfF]{0,2})(?:'+DQ3+'[\\s\\S]*?'+DQ3+'|'+SQ3+'[\\s\\S]*?'+SQ3+')','y');
var RE_STR=new RegExp('(?:[rRbBfF]{0,2})(?:"(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\')','y');
var RE_CMT=/#.*/y;
var RE_DECOR=/@[\w.]+/y;
var RE_NUM=/(?:0[xX][\da-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|(?:\d[.\d_]*)(?:[eE][+-]?\d+)?)(?:jJ)?/y;
var RE_IDENT=/[a-zA-Z_]\w*/y;
var RE_OP=/(?::=|\*\*|\/\/|<<|>>|->|==|!=|<=|>=|[+\-*/%&|^~<>!=])/y;
var RE_PUNCT=/[(){}\[\],;:.@]/y;
function highlightPython(code){
  var out=[],i=0,len=code.length,lastKW='';
  function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function span(cls,txt){out.push('<span class="',cls,'">',esc(txt),'</span>');}
  function plain(txt){out.push(esc(txt));}
  function tryRx(rx){
    rx.lastIndex=i;
    var m=rx.exec(code);
    if(m&&m.index===i)return m[0];
    return null;
  }
  while(i<len){
    var ch=code[i];
    if(ch===' '||ch==='\t'||ch==='\n'||ch==='\r'){
      var j=i+1;while(j<len&&' \t\n\r'.indexOf(code[j])>=0)j++;
      plain(code.substring(i,j));i=j;continue;
    }
    var m=tryRx(RE_TRI);
    if(m){span('ss',m);i+=m.length;lastKW='';continue;}
    m=tryRx(RE_STR);
    if(m){span('ss',m);i+=m.length;lastKW='';continue;}
    m=tryRx(RE_CMT);
    if(m){span('sc',m);i+=m.length;lastKW='';continue;}
    if(ch==='@'){
      var prev=code.substring(Math.max(0,i-30),i);
      if(/(?:^|[\n\r])\s*$/.test(prev)){
        m=tryRx(RE_DECOR);
        if(m){span('sd',m);i+=m.length;lastKW='';continue;}
      }
    }
    if((ch>='0'&&ch<='9')&&!(i>0&&/[a-zA-Z_]/.test(code[i-1]))){
      m=tryRx(RE_NUM);
      if(m){span('sn',m);i+=m.length;lastKW='';continue;}
    }
    if(/[a-zA-Z_]/.test(ch)){
      m=tryRx(RE_IDENT);
      if(m){
        if(KW_SET.has(m)){span('sk',m);lastKW=m;}
        else if(m==='self'||m==='cls'){span('sl',m);lastKW='';}
        else if(m==='True'||m==='False'||m==='None'||m==='__name__'||m==='__all__'||m==='__doc__'||m==='__init__'){span('sb',m);lastKW='';}
        else if(BI_SET.has(m)){span('sb',m);lastKW='';}
        else if(lastKW==='def'){span('sf',m);lastKW='';}
        else if(lastKW==='class'){span('st',m);lastKW='';}
        else{plain(m);lastKW='';}
        i+=m.length;continue;
      }
    }
    m=tryRx(RE_OP);
    if(m){span('so',m);i+=m.length;lastKW='';continue;}
    m=tryRx(RE_PUNCT);
    if(m){span('sp',m);i+=m.length;lastKW='';continue;}
    plain(ch);i++;lastKW='';
  }
  return out.join('');
}

// ========== Source Panel ==========
function openSource(item){
    document.getElementById('sp-title').textContent=item.full_name||item.id;
    var code=SD[item.full_name||item.id]||'(Source not available)';
    document.getElementById('sp-code').innerHTML=highlightPython(code);
    document.getElementById('source-panel').classList.add('open');
}
document.getElementById('sp-close').addEventListener('click',function(){var sp=document.getElementById('source-panel');sp.classList.remove('open');sp.style.width='';});
var spEl=document.getElementById('source-panel');
var spResize=document.getElementById('sp-resize');
var spResizing=false,spRSx=0,spRSw=0;
spResize.addEventListener('mousedown',function(e){e.preventDefault();e.stopPropagation();spResizing=true;spRSx=e.clientX;spRSw=spEl.offsetWidth;spEl.style.transition='none';});
window.addEventListener('mousemove',function(e){if(!spResizing)return;var dx=spRSx-e.clientX;var nw=Math.max(280,Math.min(window.innerWidth*0.7,spRSw+dx));spEl.style.width=nw+'px';});
window.addEventListener('mouseup',function(){if(spResizing){spResizing=false;spEl.style.transition='';}});

// ========== Measure ==========
function measure(){
    if(!posDirty&&posCache)return posCache;
    var pos={},cr=cvs.getBoundingClientRect();
    var els=cvs.querySelectorAll('[data-id]');
    for(var i=0;i<els.length;i++){
        var el=els[i],id=el.dataset.id,r=el.getBoundingClientRect();
        pos[id]={x:(r.left-cr.left)/scale,y:(r.top-cr.top)/scale,
            w:r.width/scale,h:r.height/scale,
            cx:(r.left-cr.left+r.width/2)/scale,cy:(r.top-cr.top+r.height/2)/scale};
    }
    posCache=pos;posDirty=false;return pos;
}
function invalidateMeasure(){posDirty=true;}

function getViewport(){var cr=ctnr.getBoundingClientRect();return{left:-panX/scale-200,top:-panY/scale-200,right:(cr.width-panX)/scale+200,bottom:(cr.height-panY)/scale+200};}

// ========== Edge Drawing ==========
var edgeDrawPending=false;
function scheduleDrawEdges(){if(edgeDrawPending)return;edgeDrawPending=true;requestAnimationFrame(function(){edgeDrawPending=false;drawEdges();});}

function drawEdges(){
    svg.innerHTML='';
    var oldDots=cvs.querySelectorAll('.conn-dot');for(var di=0;di<oldDots.length;di++)oldDots[di].remove();
    if(!showEdges&&!showFlow)return;
    var defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
    var mk1=document.createElementNS('http://www.w3.org/2000/svg','marker');mk1.setAttribute('id','a-call');mk1.setAttribute('viewBox','0 0 12 8');mk1.setAttribute('refX','11');mk1.setAttribute('refY','4');mk1.setAttribute('markerWidth','10');mk1.setAttribute('markerHeight','7');mk1.setAttribute('orient','auto');var p1=document.createElementNS('http://www.w3.org/2000/svg','path');p1.setAttribute('d','M0,0 L12,4 L0,8 Z');p1.setAttribute('fill','#5b9bd5');mk1.appendChild(p1);defs.appendChild(mk1);
    var mk1h=document.createElementNS('http://www.w3.org/2000/svg','marker');mk1h.setAttribute('id','a-call-hl');mk1h.setAttribute('viewBox','0 0 12 8');mk1h.setAttribute('refX','11');mk1h.setAttribute('refY','4');mk1h.setAttribute('markerWidth','10');mk1h.setAttribute('markerHeight','7');mk1h.setAttribute('orient','auto');var p1h=document.createElementNS('http://www.w3.org/2000/svg','path');p1h.setAttribute('d','M0,0 L12,4 L0,8 Z');p1h.setAttribute('fill','#7db8f0');mk1h.appendChild(p1h);defs.appendChild(mk1h);
    var mk2=document.createElementNS('http://www.w3.org/2000/svg','marker');mk2.setAttribute('id','a-inh');mk2.setAttribute('viewBox','0 0 12 8');mk2.setAttribute('refX','11');mk2.setAttribute('refY','4');mk2.setAttribute('markerWidth','10');mk2.setAttribute('markerHeight','7');mk2.setAttribute('orient','auto');var p2=document.createElementNS('http://www.w3.org/2000/svg','path');p2.setAttribute('d','M0,0 L12,4 L0,8 Z');p2.setAttribute('fill','#0d0e1a');p2.setAttribute('stroke','#ff6b6b');p2.setAttribute('stroke-width','1.5');mk2.appendChild(p2);defs.appendChild(mk2);
    var mk2h=document.createElementNS('http://www.w3.org/2000/svg','marker');mk2h.setAttribute('id','a-inh-hl');mk2h.setAttribute('viewBox','0 0 12 8');mk2h.setAttribute('refX','11');mk2h.setAttribute('refY','4');mk2h.setAttribute('markerWidth','10');mk2h.setAttribute('markerHeight','7');mk2h.setAttribute('orient','auto');var p2h=document.createElementNS('http://www.w3.org/2000/svg','path');p2h.setAttribute('d','M0,0 L12,4 L0,8 Z');p2h.setAttribute('fill','#1a0e1a');p2h.setAttribute('stroke','#ff9090');p2h.setAttribute('stroke-width','2');mk2h.appendChild(p2h);defs.appendChild(mk2h);
    var mkf=document.createElementNS('http://www.w3.org/2000/svg','marker');mkf.setAttribute('id','a-flow');mkf.setAttribute('viewBox','0 0 12 8');mkf.setAttribute('refX','11');mkf.setAttribute('refY','4');mkf.setAttribute('markerWidth','9');mkf.setAttribute('markerHeight','7');mkf.setAttribute('orient','auto');var pf=document.createElementNS('http://www.w3.org/2000/svg','path');pf.setAttribute('d','M0,0 L12,4 L0,8 Z');pf.setAttribute('fill','#f5a623');mkf.appendChild(pf);defs.appendChild(mkf);
    // Per-depth flow arrowheads (one per palette color) so the arrowhead matches
    // the depth-colored stroke. id a-flow-d<idx>.
    for(var dci=0;dci<DEPTH_COLORS.length;dci++){var md=document.createElementNS('http://www.w3.org/2000/svg','marker');md.setAttribute('id','a-flow-d'+dci);md.setAttribute('viewBox','0 0 12 8');md.setAttribute('refX','11');md.setAttribute('refY','4');md.setAttribute('markerWidth','9');md.setAttribute('markerHeight','7');md.setAttribute('orient','auto');var pd=document.createElementNS('http://www.w3.org/2000/svg','path');pd.setAttribute('d','M0,0 L12,4 L0,8 Z');pd.setAttribute('fill',DEPTH_COLORS[dci]);md.appendChild(pd);defs.appendChild(md);}
    svg.appendChild(defs);
    var pos=measure(),vp=getViewport();
    var visibleIds=new Set();Object.keys(pos).forEach(function(id){var p=pos[id];if(p.x+p.w>vp.left&&p.x<vp.right&&p.y+p.h>vp.top&&p.y<vp.bottom)visibleIds.add(id);});
    function isBlockVisible(id){if(visibleIds.has(id))return true;var parts=id.split('.');for(var i=parts.length-1;i>0;i--){if(visibleIds.has(parts.slice(0,i).join('.')))return true;}return false;}
    var visible=[];
    if(showCalls)ED.forEach(function(e){if(isBlockVisible(e.source)||isBlockVisible(e.target)){var sp=findPos(e.source,pos),tp=findPos(e.target,pos);if(sp&&tp)visible.push({s:e.source,t:e.target,type:'call'});}});
    if(showInherit)ID.forEach(function(e){if(isBlockVisible(e.source)||isBlockVisible(e.target)){var sp=findPos(e.source,pos),tp=findPos(e.target,pos);if(sp&&tp)visible.push({s:e.source,t:e.target,type:'inherit'});}});
    if(maxEdges>0&&visible.length>maxEdges){visible=randomSample(visible,maxEdges);}
    var BATCH=80,idx=0;function batch(){var end=Math.min(idx+BATCH,visible.length);for(;idx<end;idx++){var e=visible[idx];createEdgePath(e.s,e.t,e.type,pos);}if(idx<visible.length)requestAnimationFrame(batch);}
    if(visible.length>0)batch();
    if(showFlow)drawFlowEdges(pos,vp,visibleIds,isBlockVisible);
    var maxX=0,maxY=0;Object.keys(pos).forEach(function(id){var p=pos[id];maxX=Math.max(maxX,p.x+p.w+200);maxY=Math.max(maxY,p.y+p.h+200);});svg.setAttribute('width',maxX);svg.setAttribute('height',maxY);
}

// Numbered execution-flow arrows (amber) with a step-number badge at the midpoint.
// The draw set is cached (keyed by maxEdges|flowDepthFilter) so the random sample
// does not reshuffle on every pan/zoom — only viewport visibility is rechecked per
// draw. maxEdges limits flow arrows exactly like static edges (random sampling
// over the limit); 0 = unlimited (with a browser-safety backstop).
var FLOW_HARD=8000;
var flowDrawSet=null,flowDrawKey='';
function ensureFlowDrawSet(){
    var key=maxEdges+'|'+flowDepthFilter;
    if(flowDrawSet&&flowDrawKey===key)return flowDrawSet;
    flowDrawKey=key;
    var f;
    if(flowDepthFilter==='all'){f=flowEdges.slice();}
    else{f=flowEdges.filter(function(e){var d=stepDepth[e.step];return d!==undefined&&d<=flowDepthFilter;});}
    if(maxEdges>0&&f.length>maxEdges)f=randomSample(f,maxEdges);
    else if(f.length>FLOW_HARD)f=randomSample(f,FLOW_HARD);
    flowDrawSet=f;return f;
}
function drawFlowEdges(pos,vp,visibleIds,isBlockVisible){
    if(!flowEdges||flowEdges.length===0)return;
    var set=ensureFlowDrawSet();if(!set.length)return;
    var idx=0;
    function batch(){
        var end=Math.min(idx+150,set.length);
        for(;idx<end;idx++){var e=set[idx];if(!isBlockVisible(e.s)&&!isBlockVisible(e.t))continue;createFlowEdge(e.s,e.t,e.step,pos);}
        if(idx<set.length)requestAnimationFrame(batch);
    }
    batch();
}
function createFlowEdge(sourceId,targetId,step,pos){
    var sPos=findPos(sourceId,pos),tPos=findPos(targetId,pos);if(!sPos||!tPos)return;
    var srcPt=rectEdgePoint(sPos,tPos.cx,tPos.cy),tgtPt=rectEdgePoint(tPos,sPos.cx,sPos.cy);
    var x1=srcPt.x,y1=srcPt.y,x2=tgtPt.x,y2=tgtPt.y;var dx=x2-x1,dy=y2-y1,cx1,cy1,cx2,cy2;
    if(Math.abs(dy)>Math.abs(dx)*0.4){cx1=x1;cy1=y1+dy*0.35;cx2=x2;cy2=y2-dy*0.35;var off=(Math.sin(x1*0.01+y1*0.01)*0.5+0.5)*24-12;cx1+=off;cx2+=off;}
    else{cx1=x1+dx*0.35;cy1=y1;cx2=x1+dx*0.65;cy2=y2;var offy=(Math.sin(x1*0.01+y1*0.01)*0.5+0.5)*24-12;cy1+=offy;cy2+=offy;}
    var d='M'+x1+','+y1+' C'+cx1+','+cy1+' '+cx2+','+cy2+' '+x2+','+y2;
    var path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',d);path.classList.add('ef');var dci=depthColorIdx(stepDepth[step]),dcol=DEPTH_COLORS[dci];path.style.stroke=dcol;path.setAttribute('marker-end','url(#a-flow-d'+dci+')');path.dataset.source=sourceId;path.dataset.target=targetId;svg.appendChild(path);
    var lbl=''+step;var w=lbl.length*7+8,mx=(x1+x2)/2,my=(y1+y2)/2;
    var g=document.createElementNS('http://www.w3.org/2000/svg','g');g.classList.add('flow-label');
    var rect=document.createElementNS('http://www.w3.org/2000/svg','rect');rect.setAttribute('x',mx-w/2);rect.setAttribute('y',my-8);rect.setAttribute('width',w);rect.setAttribute('height',16);rect.setAttribute('rx',4);rect.style.stroke=dcol;g.appendChild(rect);
    var tx=document.createElementNS('http://www.w3.org/2000/svg','text');tx.setAttribute('x',mx);tx.setAttribute('y',my);tx.textContent=lbl;tx.style.fill=dcol;g.appendChild(tx);
    svg.appendChild(g);
}

function rectEdgePoint(rect,tx,ty){var cx=rect.cx,cy=rect.cy,dx=tx-cx,dy=ty-cy;if(dx===0&&dy===0)return{x:cx,y:cy};var candidates=[];if(dy!==0){var t=(rect.y-cy)/dy;if(t>0){var ix=cx+dx*t;if(ix>=rect.x&&ix<=rect.x+rect.w)candidates.push({x:ix,y:rect.y,t:t});}}if(dy!==0){var t2=(rect.y+rect.h-cy)/dy;if(t2>0){var ix2=cx+dx*t2;if(ix2>=rect.x&&ix2<=rect.x+rect.w)candidates.push({x:ix2,y:rect.y+rect.h,t:t2});}}if(dx!==0){var t3=(rect.x-cx)/dx;if(t3>0){var iy=cy+dy*t3;if(iy>=rect.y&&iy<=rect.y+rect.h)candidates.push({x:rect.x,y:iy,t:t3});}}if(dx!==0){var t4=(rect.x+rect.w-cx)/dx;if(t4>0){var iy2=cy+dy*t4;if(iy2>=rect.y&&iy2<=rect.y+rect.h)candidates.push({x:rect.x+rect.w,y:iy2,t:t4});}}if(candidates.length===0)return{x:cx,y:cy};candidates.sort(function(a,b){return a.t-b.t;});return candidates[0];}

function findPos(id,pos){if(pos[id])return pos[id];var parts=id.split('.');for(var i=parts.length-1;i>0;i--){var c=parts.slice(0,i).join('.');if(pos[c])return pos[c];}return null;}

function createEdgePath(sourceId,targetId,type,pos){
    var sPos=findPos(sourceId,pos),tPos=findPos(targetId,pos);if(!sPos||!tPos)return;
    var srcPt=rectEdgePoint(sPos,tPos.cx,tPos.cy),tgtPt=rectEdgePoint(tPos,sPos.cx,sPos.cy);
    var x1=srcPt.x,y1=srcPt.y,x2=tgtPt.x,y2=tgtPt.y;var dx=x2-x1,dy=y2-y1,cx1,cy1,cx2,cy2;
    if(Math.abs(dy)>Math.abs(dx)*0.4){cx1=x1;cy1=y1+dy*0.35;cx2=x2;cy2=y2-dy*0.35;var offset=(Math.sin(x1*0.01+y1*0.01)*0.5+0.5)*30-15;cx1+=offset;cx2+=offset;}
    else{cx1=x1+dx*0.35;cy1=y1;cx2=x1+dx*0.65;cy2=y2;var offy=(Math.sin(x1*0.01+y1*0.01)*0.5+0.5)*30-15;cy1+=offy;cy2+=offy;}
    var d='M'+x1+','+y1+' C'+cx1+','+cy1+' '+cx2+','+cy2+' '+x2+','+y2;
    var hit=document.createElementNS('http://www.w3.org/2000/svg','path');hit.setAttribute('d',d);hit.classList.add('e-hit');hit.setAttribute('stroke','transparent');hit.setAttribute('stroke-width','14');hit.setAttribute('fill','none');hit.dataset.source=sourceId;hit.dataset.target=targetId;hit.dataset.edgeType=type;svg.appendChild(hit);
    var path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',d);path.classList.add('e');var isCall=type==='call';path.setAttribute('stroke',isCall?'#5b9bd540':'#ff6b6b40');path.setAttribute('stroke-width','1.5');path.setAttribute('fill','none');path.setAttribute('marker-end',isCall?'url(#a-call)':'url(#a-inh)');if(!isCall)path.setAttribute('stroke-dasharray','8,5');path.dataset.source=sourceId;path.dataset.target=targetId;path.dataset.edgeType=type;svg.appendChild(path);
    createDot(x1,y1,targetId,sourceId,type);createDot(x2,y2,sourceId,targetId,type);
}

function createDot(x,y,navId,fromId,type){
    var dot=document.createElement('div');dot.className='conn-dot '+(type==='call'?'call':'inherit');dot.style.left=(x-4)+'px';dot.style.top=(y-4)+'px';dot.dataset.navId=navId;dot.dataset.fromId=fromId;
    var navShort=navId.split('.').slice(-2).join('.');dot.title='→ '+navShort;
    dot.addEventListener('pointerenter',function(ev){showTip(ev,'→ '+navShort);var pos=measure(),p=pos[navId];if(!p){var parts=navId.split('.');for(var i=parts.length-1;i>0;i--){var c=parts.slice(0,i).join('.');if(pos[c]){p=pos[c];break;}}}if(p)hlBlocks(navId);});
    dot.addEventListener('pointerleave',function(){hideTip();clearBlockHL();});cvs.appendChild(dot);
}

function highlightEdge(el,on){var isCall=el.dataset.edgeType==='call';if(on){el.setAttribute('stroke-width','2.5');el.setAttribute('stroke',isCall?'#7db8f0':'#ff9090');el.setAttribute('marker-end',isCall?'url(#a-call-hl)':'url(#a-inh-hl)');var hit=el.previousElementSibling;if(hit&&hit.classList.contains('e-hit')){hit.parentNode.appendChild(hit);}el.parentNode.appendChild(el);}else{el.setAttribute('stroke-width','1.5');el.setAttribute('stroke',isCall?'#5b9bd540':'#ff6b6b40');el.setAttribute('marker-end',isCall?'url(#a-call)':'url(#a-inh)');if(!isCall)el.setAttribute('stroke-dasharray','8,5');}}
function isLockedConnected(el){if(!lockedId)return false;var s=el.dataset.source,t=el.dataset.target;return s===lockedId||t===lockedId||s.startsWith(lockedId+'.')||t.startsWith(lockedId+'.');}

function hlBlocks(){for(var i=0;i<arguments.length;i++){var id=arguments[i];var el=document.querySelector('[data-id="'+id+'"]');if(!el){var parts=id.split('.');for(var j=parts.length-1;j>0;j--){var c=parts.slice(0,j).join('.');el=document.querySelector('[data-id="'+c+'"]');if(el)break;}}if(el)el.classList.add('block-hl');}}
function clearBlockHL(){document.querySelectorAll('.block-hl').forEach(function(e){e.classList.remove('block-hl');});}

function resolveEdge(el){if(el.classList.contains('e-hit')){var next=el.nextElementSibling;if(next&&next.classList.contains('e'))return next;}if(el.classList.contains('e'))return el;return null;}
function findEdgeFromTarget(ev){var hit=ev.target.closest('.e-hit');if(hit)return resolveEdge(hit);var e=ev.target.closest('.e');return e||null;}

svg.addEventListener('pointerenter',function(ev){var p=findEdgeFromTarget(ev);if(!p)return;highlightEdge(p,true);var sn=p.dataset.source.split('.').slice(-2).join('.');var tn=p.dataset.target.split('.').slice(-2).join('.');showTip(ev,sn+' → '+tn);hlBlocks(p.dataset.source,p.dataset.target);},true);
svg.addEventListener('pointermove',function(ev){if(findEdgeFromTarget(ev)){tip.style.left=(ev.clientX+14)+'px';tip.style.top=(ev.clientY+14)+'px';}},true);
svg.addEventListener('pointerleave',function(ev){var p=findEdgeFromTarget(ev);if(!p)return;if(!isLockedConnected(p))highlightEdge(p,false);hideTip();clearBlockHL();},true);

function navigateToBlock(id){var pos=measure(),p=pos[id];if(!p){var parts=id.split('.');for(var i=parts.length-1;i>0;i--){var c=parts.slice(0,i).join('.');if(pos[c]){p=pos[c];break;}}}if(!p)return;var cr=ctnr.getBoundingClientRect();panX=cr.width/2-p.cx*scale;panY=cr.height/2-p.cy*scale;updateTx();scheduleDrawEdges();updateMinimap();}

cvs.addEventListener('mouseover',function(e){var block=e.target.closest('[data-id]');if(!block)return;hideTip();var id=block.dataset.id;highlightEdgesFor(id,true);hlBlocks(id);});
cvs.addEventListener('mouseout',function(e){var block=e.target.closest('[data-id]');if(!block)return;var id=block.dataset.id;if(id===lockedId)return;highlightEdgesFor(id,false);clearBlockHL();if(lockedId){highlightEdgesFor(lockedId,true);hlBlocks(lockedId);}});

var sourceMap={};
function buildSourceMap(node){if(node.type==='package'){node.children.forEach(buildSourceMap);return;}if(node.type==='module'){node.children.forEach(function(ch){sourceMap[ch.full_name||ch.id]=ch;if(ch.children){ch.children.forEach(function(mt){sourceMap[mt.full_name||mt.id]=mt;});}});}}
if(ND.type==='package'){buildSourceMap(ND);}else{ND.forEach(function(m){m.children.forEach(function(ch){sourceMap[ch.full_name||ch.id]=ch;if(ch.children){ch.children.forEach(function(mt){sourceMap[mt.full_name||mt.id]=mt;});}});});}

cvs.addEventListener('click',function(e){
    var dot=e.target.closest('.conn-dot');if(dot){e.stopPropagation();navigateToBlock(dot.dataset.navId);return;}
    var block=e.target.closest('[data-id]');
    if(block){var id=block.dataset.id;if(lockedId&&lockedId!==id){clearAllEdgeHL();clearBlockHL();}lockedId=id;highlightEdgesFor(id,true);hlBlocks(id);var item=sourceMap[id];if(item){openSource(item);}else{document.getElementById('sp-title').textContent=id;var code=SD[id]||'(Source not available)';document.getElementById('sp-code').innerHTML=highlightPython(code);document.getElementById('source-panel').classList.add('open');}e.stopPropagation();return;}
    if(lockedId){lockedId=null;clearAllEdgeHL();clearBlockHL();}
});

function highlightEdgesFor(id,on){
    if(on){
        // When package edges are hidden and this is a hidden-depth package block, skip
        if(isPkgEdgeHidden(id,id))return;
        // Highlight existing drawn edges connected to this block
        var paths=svg.querySelectorAll('.e');
        var existingKeys={};
        for(var i=0;i<paths.length;i++){
            var p=paths[i],s=p.dataset.source,t=p.dataset.target;
            existingKeys[s+'|'+t+'|'+p.dataset.edgeType]=true;
            var connected=s===id||t===id||s.startsWith(id+'.')||t.startsWith(id+'.');
            if(connected){
                // Skip package edges based on level filter
                if(isPkgEdgeHidden(s,t))continue;
                highlightEdge(p,true);
                var hit=p.previousElementSibling;
                if(hit&&hit.classList.contains('e-hit'))hit.parentNode.appendChild(hit);
                p.parentNode.appendChild(p);
            }
        }
        // Bypass maxEdges sampling: temporarily draw every edge connected to this block
        var pos=measure();
        function addTemp(s,t,type){
            var key=s+'|'+t+'|'+type;
            if(existingKeys[key])return;
            var connected=s===id||t===id||s.startsWith(id+'.')||t.startsWith(id+'.');
            if(!connected)return;
            // Skip package edges based on level filter
            if(isPkgEdgeHidden(s,t))return;
            if(!findPos(s,pos)||!findPos(t,pos))return;
            var svgChildBefore=svg.children.length;
            var dotCountBefore=cvs.querySelectorAll('.conn-dot').length;
            createEdgePath(s,t,type,pos);
            for(var k=svgChildBefore;k<svg.children.length;k++){
                var ch=svg.children[k];
                if(ch.classList&&(ch.classList.contains('e')||ch.classList.contains('e-hit'))){
                    ch.classList.add('e-temp');
                    ch.dataset.tempFor=id;
                }
            }
            var dots=cvs.querySelectorAll('.conn-dot');
            for(var k=dotCountBefore;k<dots.length;k++){
                dots[k].classList.add('temp');
                dots[k].dataset.tempFor=id;
            }
            var newPath=svg.lastElementChild;
            if(newPath&&newPath.classList.contains('e'))highlightEdge(newPath,true);
            existingKeys[key]=true;
        }
        if(showCalls)ED.forEach(function(e){addTemp(e.source,e.target,'call');});
        if(showInherit)ID.forEach(function(e){addTemp(e.source,e.target,'inherit');});
    }else{
        // Remove temp edges/dots that were added for this block only
        var temps=svg.querySelectorAll('.e-temp');
        for(var i=0;i<temps.length;i++)if(temps[i].dataset.tempFor===id)temps[i].remove();
        var tempDots=cvs.querySelectorAll('.conn-dot.temp');
        for(var i=0;i<tempDots.length;i++)if(tempDots[i].dataset.tempFor===id)tempDots[i].remove();
        var paths=svg.querySelectorAll('.e');
        for(var i=0;i<paths.length;i++){
            var p=paths[i],s=p.dataset.source,t=p.dataset.target;
            var connected=s===id||t===id||s.startsWith(id+'.')||t.startsWith(id+'.');
            if(connected)highlightEdge(p,false);
        }
    }
}
function clearAllEdgeHL(){
    var temps=svg.querySelectorAll('.e-temp');for(var i=0;i<temps.length;i++)temps[i].remove();
    var tempDots=cvs.querySelectorAll('.conn-dot.temp');for(var i=0;i<tempDots.length;i++)tempDots[i].remove();
    var paths=svg.querySelectorAll('.e');for(var i=0;i<paths.length;i++)highlightEdge(paths[i],false);
}

function showTip(e,text){tip.textContent=text;tip.style.display='block';tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';}
function hideTip(){tip.style.display='none';}

function updateTx(){cvs.style.transform='translate('+panX+'px,'+panY+'px) scale('+scale+')';zoomEl.textContent=Math.round(scale*100)+'%';}

ctnr.addEventListener('mousedown',function(e){if(e.target.closest('[data-id]')||e.target.closest('.e')||e.target.closest('.e-hit'))return;closeNavMenu();dragging=true;dsx=e.clientX;dsy=e.clientY;psx=panX;psy=panY;ctnr.classList.add('grabbing');});
window.addEventListener('mousemove',function(e){if(!dragging)return;panX=psx+(e.clientX-dsx);panY=psy+(e.clientY-dsy);updateTx();scheduleDrawEdges();updateMinimap();});
window.addEventListener('mouseup',function(){dragging=false;ctnr.classList.remove('grabbing');});

ctnr.addEventListener('wheel',function(e){e.preventDefault();var d=e.deltaY>0?0.92:1.08;var ns=Math.max(0.12,Math.min(3,scale*d));var r=ctnr.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;panX=mx-(mx-panX)*(ns/scale);panY=my-(my-panY)*(ns/scale);scale=ns;updateTx();invalidateMeasure();scheduleDrawEdges();updateMinimap();},{passive:false});

function fitToScreen(){
    invalidateMeasure();var cr=ctnr.getBoundingClientRect(),pos=measure();
    var minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
    Object.keys(pos).forEach(function(id){var p=pos[id];minX=Math.min(minX,p.x);minY=Math.min(minY,p.y);maxX=Math.max(maxX,p.x+p.w);maxY=Math.max(maxY,p.y+p.h);});
    if(minX===Infinity)return;var cw=maxX-minX+100,ch=maxY-minY+100;
    scale=Math.min(cr.width/cw,cr.height/ch,1.0);scale=Math.max(0.12,scale);
    panX=(cr.width-cw*scale)/2-minX*scale+50*scale;panY=(cr.height-ch*scale)/2-minY*scale+50*scale;
    updateTx();updateMinimap();
}

document.getElementById('btn-fit').addEventListener('click',fitToScreen);
document.getElementById('btn-flow').addEventListener('click',function(){showFlow=!showFlow;this.classList.toggle('active',showFlow);scheduleDrawEdges();});
document.getElementById('btn-entryend').addEventListener('click',function(){showEntryEnd=!showEntryEnd;this.classList.toggle('active',showEntryEnd);document.body.classList.toggle('hide-entryend',!showEntryEnd);});
document.getElementById('btn-external').addEventListener('click',function(){showExternal=!showExternal;this.classList.toggle('active',showExternal);var p=document.getElementById('external-panel');if(showExternal){renderExternal();p.classList.add('open');}else{p.classList.remove('open');}});
document.getElementById('btn-uncalled').addEventListener('click',function(){hideUncalled=!hideUncalled;this.classList.toggle('active',hideUncalled);document.body.classList.toggle('hide-uncalled',hideUncalled);invalidateMeasure();setTimeout(function(){drawEdges();fitToScreen();updateMinimap();},60);});
document.getElementById('btn-edges').addEventListener('click',function(){showEdges=!showEdges;this.classList.toggle('active',showEdges);if(!showEdges){document.getElementById('btn-calls').classList.remove('active');document.getElementById('btn-inherit').classList.remove('active');}else{if(showCalls)document.getElementById('btn-calls').classList.add('active');if(showInherit)document.getElementById('btn-inherit').classList.add('active');}scheduleDrawEdges();});
document.getElementById('btn-calls').addEventListener('click',function(){if(!showEdges){showEdges=true;document.getElementById('btn-edges').classList.add('active');}showCalls=!showCalls;this.classList.toggle('active',showCalls);scheduleDrawEdges();});
document.getElementById('btn-inherit').addEventListener('click',function(){if(!showEdges){showEdges=true;document.getElementById('btn-edges').classList.add('active');}showInherit=!showInherit;this.classList.toggle('active',showInherit);scheduleDrawEdges();});
document.getElementById('pkg-level').addEventListener('change',function(){
  pkgEdgeLevel=this.value;
});
document.getElementById('flow-depth').addEventListener('change',function(){
  flowDepthFilter=this.value==='all'?'all':parseInt(this.value);
  scheduleDrawEdges();
});
document.getElementById('btn-top').addEventListener('click',function(){edgesOnTop=!edgesOnTop;this.classList.toggle('active',edgesOnTop);document.body.classList.toggle('edges-on-top',edgesOnTop);});
document.getElementById('btn-collapse').addEventListener('click',function(){
    allCollapsed=!allCollapsed;this.classList.toggle('active',allCollapsed);
    document.querySelectorAll('.class-body,.class-params').forEach(function(e){e.style.display=allCollapsed?'none':'';});
    document.querySelectorAll('.func-signature,.method-signature').forEach(function(e){e.style.display=allCollapsed?'none':'';});
    invalidateMeasure();setTimeout(function(){drawEdges();fitToScreen();updateMinimap();},60);
});

var searchTimer;
document.getElementById('search-box').addEventListener('input',function(e){clearTimeout(searchTimer);searchTimer=setTimeout(function(){searchTerm=e.target.value.toLowerCase().trim();clearSearch();if(!searchTerm)return;document.querySelectorAll('[data-id]').forEach(function(el){var id=(el.dataset.id||'').toLowerCase(),txt=el.textContent.toLowerCase();if(id.indexOf(searchTerm)>=0||txt.indexOf(searchTerm)>=0)el.classList.add('search-hit');});var first=document.querySelector('.search-hit');if(first){var id=first.dataset.id,pos=measure(),p=pos[id];if(p){var cr=ctnr.getBoundingClientRect();panX=cr.width/2-p.cx*scale;panY=cr.height/2-p.cy*scale;updateTx();updateMinimap();}}},200);});
function clearSearch(){document.querySelectorAll('.search-hit').forEach(function(e){e.classList.remove('search-hit');});}

var mmEl=document.getElementById('minimap');var mmCanvas=document.getElementById('mm-canvas');var mmVp=document.getElementById('mm-viewport');var mmCtx=mmCanvas.getContext('2d');
function getMmSize(){var r=mmEl.getBoundingClientRect();return{w:r.width,h:r.height};}

function drawMinimapBlocks(node,pos,mmScale,offX,offY){
    if(node.type==='package'){var p=pos[node.id];if(p){var mx=p.x*mmScale+offX,my=p.y*mmScale+offY;var mw=p.w*mmScale,mh=p.h*mmScale;mmCtx.fillStyle='#6a5a2a40';mmCtx.fillRect(mx,my,Math.max(mw,1),Math.max(mh,1));mmCtx.strokeStyle='#6a5a2a30';mmCtx.lineWidth=0.5;mmCtx.strokeRect(mx,my,Math.max(mw,1),Math.max(mh,1));}node.children.forEach(function(ch){drawMinimapBlocks(ch,pos,mmScale,offX,offY);});}
    else if(node.type==='module'){var p=pos[node.id];if(p){var mx=p.x*mmScale+offX,my=p.y*mmScale+offY;var mw=p.w*mmScale,mh=p.h*mmScale;mmCtx.fillStyle='#3e428060';mmCtx.fillRect(mx,my,Math.max(mw,1),Math.max(mh,1));mmCtx.strokeStyle='#3e428040';mmCtx.lineWidth=0.5;mmCtx.strokeRect(mx,my,Math.max(mw,1),Math.max(mh,1));}}
}

function updateMinimap(){
    var mmSz=getMmSize(),mmW=mmSz.w,mmH=mmSz.h;var dpr=window.devicePixelRatio||1;mmCanvas.width=mmW*dpr;mmCanvas.height=mmH*dpr;mmCtx.setTransform(dpr,0,0,dpr,0,0);
    var pos=measure();var ids=Object.keys(pos);if(ids.length===0)return;
    var minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;ids.forEach(function(id){var p=pos[id];minX=Math.min(minX,p.x);minY=Math.min(minY,p.y);maxX=Math.max(maxX,p.x+p.w);maxY=Math.max(maxY,p.y+p.h);});
    var contentW=maxX-minX+40,contentH=maxY-minY+40;var mmScale=Math.min(mmW/contentW,mmH/contentH);var offX=(mmW-contentW*mmScale)/2-minX*mmScale;var offY=(mmH-contentH*mmScale)/2-minY*mmScale;
    mmCtx.fillStyle='#0d0e1a';mmCtx.fillRect(0,0,mmW,mmH);
    if(ND.type==='package'){drawMinimapBlocks(ND,pos,mmScale,offX,offY);}else{ND.forEach(function(m){var p=pos[m.id];if(!p)return;var mx=p.x*mmScale+offX,my=p.y*mmScale+offY;var mw=p.w*mmScale,mh=p.h*mmScale;mmCtx.fillStyle='#3e428060';mmCtx.fillRect(mx,my,Math.max(mw,1),Math.max(mh,1));mmCtx.strokeStyle='#3e428040';mmCtx.lineWidth=0.5;mmCtx.strokeRect(mx,my,Math.max(mw,1),Math.max(mh,1));});}
    var cr=ctnr.getBoundingClientRect();var vpLeft=(-panX/scale)*mmScale+offX;var vpTop=(-panY/scale)*mmScale+offY;var vpW=(cr.width/scale)*mmScale;var vpH=(cr.height/scale)*mmScale;
    mmVp.style.left=Math.max(0,vpLeft)+'px';mmVp.style.top=Math.max(0,vpTop)+'px';mmVp.style.width=Math.min(mmW,vpW)+'px';mmVp.style.height=Math.min(mmH,vpH)+'px';
    mmEl._map={scale:mmScale,offX:offX,offY:offY,contentScale:scale};
}

mmEl.addEventListener('click',function(e){if(mmResizing)return;if(mmEl.classList.contains('stack-open'))return;var map=mmEl._map;if(!map)return;var rect=mmEl.getBoundingClientRect();var mx=e.clientX-rect.left,my=e.clientY-rect.top;if(mx<12||my<12||mx>rect.width-12||my>rect.height-12)return;var cx=(mx-map.offX)/map.scale;var cy=(my-map.offY)/map.scale;var cr=ctnr.getBoundingClientRect();panX=cr.width/2-cx*scale;panY=cr.height/2-cy*scale;updateTx();scheduleDrawEdges();updateMinimap();});

var mmResize=document.getElementById('mm-resize');var mmResizing=false,mmRSx=0,mmRSy=0,mmRSw=0,mmRSh=0,mmRSright=0,mmRSbottom=0;
mmResize.addEventListener('mousedown',function(e){e.preventDefault();e.stopPropagation();mmResizing=true;var r=mmEl.getBoundingClientRect();mmRSx=e.clientX;mmRSy=e.clientY;mmRSw=r.width;mmRSh=r.height;mmRSright=r.right;mmRSbottom=r.bottom;});
window.addEventListener('mousemove',function(e){if(!mmResizing)return;var dx=e.clientX-mmRSx,dy=e.clientY-mmRSy;var nw=Math.max(120,mmRSw-dx),nh=Math.max(80,mmRSh-dy);mmEl.style.width=nw+'px';mmEl.style.height=nh+'px';mmEl.style.right=(window.innerWidth-mmRSright)+'px';mmEl.style.bottom=(window.innerHeight-mmRSbottom)+'px';updateMinimap();});
window.addEventListener('mouseup',function(){mmResizing=false;});
new ResizeObserver(function(){updateMinimap();}).observe(mmEl);

// ===== Minimap call-stack node list (toggle button) =====
var mmStackToggle=document.getElementById('mm-stack-toggle');
var mmStackEl=document.getElementById('mm-stack');
var mmStackClose=document.getElementById('mm-stack-close');
var mmDepthSlider=document.getElementById('mm-depth');
var mmDepthVal=document.getElementById('mm-depth-val');
var mmStackList=document.getElementById('mm-stack-list');
var mmStackPrev=null;   // saved {w,h} to restore on close
var STACK_CAP=2000;     // max rows rendered (perf guard)

// Configure the slider range from the trace's deepest call-stack depth.
mmDepthSlider.min=1;
mmDepthSlider.max=Math.max(1,maxFlowDepth);
mmDepthSlider.value=Math.max(1,maxFlowDepth);
mmDepthVal.textContent=mmDepthSlider.value;

function renderMmStack(){
    var n=parseInt(mmDepthSlider.value)||1;
    mmDepthVal.textContent=n;
    mmStackList.innerHTML='';
    if(!flowSteps.length){var em=document.createElement('div');em.className='ms-empty';em.textContent='No call stack (static view)';mmStackList.appendChild(em);return;}
    var shown=0,skipped=0;
    for(var i=0;i<flowSteps.length;i++){
        var s=flowSteps[i];
        if(s.depth>n)continue;
        if(shown>=STACK_CAP){skipped++;continue;}
        shown++;
        var row=document.createElement('div');row.className='ms-row';
        // Indent + color the row by call-stack depth (matches flow arrow color):
        // a depth-colored left border that stairsteps right as depth grows.
        row.style.marginLeft=(Math.min(s.depth-1,12)*11)+'px';
        row.style.borderLeft='3px solid '+depthColor(s.depth);
        var sp=document.createElement('span');sp.className='ms-step';sp.style.color=depthColor(s.depth);sp.textContent='#'+s.step;row.appendChild(sp);
        var nm=document.createElement('span');nm.className='ms-name';nm.textContent=shortName(s.id);nm.title=s.id;row.appendChild(nm);
        if(s.loop_count&&s.loop_count>1){var lp=document.createElement('span');lp.className='ms-loop';lp.textContent='↻×'+s.loop_count;row.appendChild(lp);}
        var dv=document.createElement('span');dv.className='ms-d';dv.textContent='d'+s.depth;row.appendChild(dv);
        (function(id){row.addEventListener('click',function(ev){ev.stopPropagation();navigateToBlock(id);});})(s.id);
        mmStackList.appendChild(row);
    }
    if(skipped>0){var more=document.createElement('div');more.className='ms-more';more.textContent='+'+skipped+' more — lower the depth';mmStackList.appendChild(more);}
}
function openMmStack(){
    if(mmEl.classList.contains('stack-open'))return;
    mmStackPrev={w:mmEl.offsetWidth,h:mmEl.offsetHeight};
    if(mmStackPrev.w<260)mmEl.style.width='260px';
    if(mmStackPrev.h<380)mmEl.style.height='380px';
    mmEl.classList.add('stack-open');mmStackToggle.classList.add('active');
    renderMmStack();
}
function closeMmStack(){
    if(!mmEl.classList.contains('stack-open'))return;
    mmEl.classList.remove('stack-open');mmStackToggle.classList.remove('active');
    if(mmStackPrev){mmEl.style.width=mmStackPrev.w+'px';mmEl.style.height=mmStackPrev.h+'px';mmStackPrev=null;}
    updateMinimap();
}
mmStackToggle.addEventListener('click',function(e){e.stopPropagation();if(mmEl.classList.contains('stack-open'))closeMmStack();else openMmStack();});
mmStackClose.addEventListener('click',function(e){e.stopPropagation();closeMmStack();});
mmDepthSlider.addEventListener('input',renderMmStack);
// Clicks inside the stack panel must not bubble to the minimap's pan handler.
mmStackEl.addEventListener('mousedown',function(e){e.stopPropagation();});

document.addEventListener('keydown',function(e){if(e.target.id==='search-box'||e.target.id==='edge-limit'){if(e.key==='Escape'){e.target.blur();}return;}if(e.key==='f'||e.key==='F')fitToScreen();else if(e.key==='Escape'){closeNavMenu();document.getElementById('source-panel').classList.remove('open');clearSearch();document.getElementById('search-box').value='';searchTerm='';lockedId=null;clearAllEdgeHL();clearBlockHL();}else if(e.key==='/'||(e.ctrlKey&&e.key==='f')){e.preventDefault();document.getElementById('search-box').focus();}});

buildAll();
// Populate package level selector based on discovered depths
(function(){
  var sel=document.getElementById('pkg-level');
  for(var d=0;d<=maxPkgDepth;d++){
    var opt=document.createElement('option');opt.value=d;opt.textContent='L'+d+'+';
    sel.appendChild(opt);
  }
  // "All" option to show all package edges
  var all=document.createElement('option');all.value='all';all.textContent='All';
  sel.appendChild(all);
})();
// Populate the flow-depth selector: "All depths" + one cumulative option per
// call-stack depth present in the trace (1..maxFlowDepth).
(function(){
  var sel=document.getElementById('flow-depth');
  for(var d=1;d<=maxFlowDepth;d++){
    var opt=document.createElement('option');opt.value=d;opt.textContent='≤'+d;
    sel.appendChild(opt);
  }
})();
// Depth -> color legend chips (mirrors the minimap node + flow arrow coloring).
// Show one chip per distinct palette color up to the trace's max depth.
(function(){
  if(maxFlowDepth<1)return;
  var row=document.getElementById('depth-legend-row');if(!row)return;
  var chips=document.getElementById('depth-chips');
  var upto=Math.min(maxFlowDepth,DEPTH_COLORS.length);
  for(var d=1;d<=upto;d++){
    var c=document.createElement('span');c.style.cssText='display:inline-flex;align-items:center;gap:3px';
    var sw=document.createElement('span');sw.style.cssText='width:9px;height:9px;border-radius:2px;background:'+depthColor(d)+';display:inline-block';
    var lb=document.createElement('span');lb.style.cssText='color:#8888aa;font-size:10px';lb.textContent=d;
    c.appendChild(sw);c.appendChild(lb);chips.appendChild(c);
  }
  row.style.display='flex';
})();
var pkgEdgeLevel='none'; // 'all'|'none'|0|1|2...
function isPkgEdgeHidden(s,t){
  if(pkgEdgeLevel==='all')return false;
  var sd=pkgDepthMap.get(s),td=pkgDepthMap.get(t);
  var sPkg=sd!==undefined,tPkg=td!==undefined;
  if(!sPkg&&!tPkg)return false; // neither is a package
  if(pkgEdgeLevel==='none')return true; // hide all package edges
  var lv=parseInt(pkgEdgeLevel);
  // Hide if any package endpoint is at depth < lv
  if(sPkg&&sd<lv)return true;
  if(tPkg&&td<lv)return true;
  return false;
}
})();"""

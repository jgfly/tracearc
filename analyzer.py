"""
Python AST-based code analyzer.

Parses Python source files to extract:
- Modules (file-level)
- Classes (with __init__ parameters)
- Functions (with signatures: name, params, return type)
- Call relationships between functions/methods
- Import relationships between modules
"""

import ast
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FuncInfo:
    """Represents a function or method."""
    name: str
    full_name: str  # module.Class.method or module.func
    params: list[str]  # parameter names with annotations
    return_type: str = ""
    lineno: int = 0
    end_lineno: int = 0
    calls: list[str] = field(default_factory=list)  # full_names of called functions
    source_code: str = ""
    is_method: bool = False
    class_name: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass
class ClassInfo:
    """Represents a class."""
    name: str
    full_name: str  # module.Class
    init_params: list[str]  # __init__ parameters
    bases: list[str]  # parent class names
    methods: list[FuncInfo] = field(default_factory=list)
    lineno: int = 0
    end_lineno: int = 0
    source_code: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass
class ModuleInfo:
    """Represents a Python module (file)."""
    name: str  # e.g. nanovllm.engine.scheduler
    file_path: str
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FuncInfo] = field(default_factory=list)
    imports: dict[str, str] = field(default_factory=dict)  # alias -> full_module_path


@dataclass
class PackageInfo:
    """Represents a Python package (directory)."""
    name: str  # e.g. "vllm.v1.engine"
    full_name: str  # same as name for packages
    children: list = field(default_factory=list)  # PackageInfo | ModuleInfo
    is_leaf: bool = False  # True if only contains modules, no sub-packages


@dataclass
class ProjectData:
    """Top-level container for the analyzed project."""
    name: str
    root_path: str
    modules: list[ModuleInfo] = field(default_factory=list)
    # call_edges: list of (caller_full_name, callee_full_name)
    call_edges: list[tuple[str, str]] = field(default_factory=list)
    # class_inheritance: list of (child_class_full_name, parent_class_full_name)
    class_inheritance: list[tuple[str, str]] = field(default_factory=list)
    # Hierarchical package tree built from module names
    root_package: Optional["PackageInfo"] = None


class CallVisitor(ast.NodeVisitor):
    """Visits function/method bodies to find function calls."""

    def __init__(self, analyzer: "ModuleAnalyzer"):
        self.analyzer = analyzer
        self.calls: list[str] = []

    def visit_Call(self, node: ast.Call):
        call_name = self._resolve_call_name(node.func)
        if call_name:
            self.calls.append(call_name)
        self.generic_visit(node)

    def _resolve_call_name(self, node: ast.expr) -> Optional[str]:
        """Try to resolve a call to a full name like 'module.Class.method'."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            parts.reverse()
            return ".".join(parts)
        elif isinstance(node, ast.Subscript):
            # Triton kernel call syntax: kernel[grid](args)
            # The callable is node.value (the kernel name before the brackets)
            return self._resolve_call_name(node.value)
        return None


class ModuleAnalyzer:
    """Analyzes a single Python module."""

    # Common Python built-in / stdlib attributes that are NOT real method calls
    # we want to skip when drawing call edges
    SKIP_ATTRS = {
        "append", "extend", "pop", "remove", "insert", "clear", "copy", "count",
        "index", "reverse", "sort", "items", "keys", "values", "get", "setdefault",
        "update", "join", "split", "strip", "lstrip", "rstrip", "replace", "encode",
        "decode", "format", "lower", "upper", "title", "startswith", "endswith",
        "isdigit", "isalpha", "isnumeric", "isinstance", "issubclass",
        "add", "discard", "union", "intersection",
        "write", "read", "close", "flush", "seek", "tell",
        "narrow", "chunk", "copy_", "to_bytes", "from_bytes",
        "float", "int", "len", "range", "enumerate", "zip", "map", "filter",
        "sum", "min", "max", "abs", "round", "sorted", "reversed",
        "print", "type", "super", "next", "iter", "getattr", "setattr", "hasattr",
    }

    def __init__(self, module_name: str, file_path: str, source_code: str):
        self.module_name = module_name
        self.file_path = file_path
        self.source_lines = source_code.splitlines()
        self.tree = ast.parse(source_code)
        self.imports: dict[str, str] = {}  # local_name -> full_module
        self.from_imports: dict[str, tuple[str, str]] = {}  # local_name -> (module, original_name)
        self.classes: list[ClassInfo] = []
        self.functions: list[FuncInfo] = []
        # Set of all known method names in this module (for filtering)
        self._known_method_names: set[str] = set()

    def analyze(self) -> ModuleInfo:
        """Analyze the module and return ModuleInfo."""
        self._collect_imports()
        self._collect_definitions()
        # After collecting definitions, build the known method name set
        for cls in self.classes:
            for m in cls.methods:
                self._known_method_names.add(m.name)
        return ModuleInfo(
            name=self.module_name,
            file_path=self.file_path,
            classes=self.classes,
            functions=self.functions,
            imports=self.imports,
        )

    def _collect_imports(self):
        """Collect import statements for name resolution."""
        # Package containing this module, used to resolve relative imports.
        pkg_parts = self.module_name.split(".")[:-1]
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    self.imports[local_name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level > 0:
                    # Resolve a relative import to an absolute dotted name.
                    # level=1 refers to this module's own package; each extra
                    # level pops one more segment off the package path.
                    base = pkg_parts[: max(0, len(pkg_parts) - (node.level - 1))]
                    module = ".".join(base + module.split(".")) if module else ".".join(base)
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    self.from_imports[local_name] = (module, alias.name)

    def _collect_definitions(self):
        """Collect class and function definitions."""
        for node in ast.iter_child_nodes(self.tree):
            if isinstance(node, ast.ClassDef):
                self.classes.append(self._analyze_class(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(self._analyze_function(node))

    def _analyze_class(self, node: ast.ClassDef) -> ClassInfo:
        """Analyze a class definition."""
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(self._format_attribute(base))

        decorators = []
        is_dataclass = False
        for dec in node.decorator_list:
            dec_name = self._get_decorator_name(dec)
            decorators.append(dec_name)
            if dec_name == "dataclass" or dec_name.endswith(".dataclass"):
                is_dataclass = True

        methods = []
        init_params = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_info = self._analyze_function(item, class_name=node.name)
                methods.append(func_info)
                if item.name == "__init__":
                    init_params = func_info.params

        # For dataclasses without explicit __init__, extract fields as init_params
        if is_dataclass and not init_params:
            init_params = self._extract_dataclass_fields(node)

        source = self._extract_source(node.lineno, node.end_lineno)

        return ClassInfo(
            name=node.name,
            full_name=f"{self.module_name}.{node.name}",
            init_params=init_params,
            bases=bases,
            methods=methods,
            lineno=node.lineno,
            end_lineno=node.end_lineno or 0,
            source_code=source,
            decorators=decorators,
        )

    def _analyze_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                          class_name: str = "") -> FuncInfo:
        """Analyze a function or method definition."""
        params = self._extract_params(node)
        return_type = self._get_annotation(node.returns)

        decorators = []
        for dec in node.decorator_list:
            decorators.append(self._get_decorator_name(dec))

        # Analyze calls within the function body
        visitor = CallVisitor(self)
        for child in node.body:
            visitor.visit(child)

        # Resolve calls to fuller names where possible, and filter noise
        resolved_calls = []
        for call in visitor.calls:
            resolved = self._resolve_call(call, class_name)
            if resolved and not self._is_noise_call(resolved):
                resolved_calls.append(resolved)

        if class_name:
            full_name = f"{self.module_name}.{class_name}.{node.name}"
        else:
            full_name = f"{self.module_name}.{node.name}"

        source = self._extract_source(node.lineno, node.end_lineno)

        return FuncInfo(
            name=node.name,
            full_name=full_name,
            params=params,
            return_type=return_type,
            lineno=node.lineno,
            end_lineno=node.end_lineno or 0,
            calls=resolved_calls,
            source_code=source,
            is_method=bool(class_name),
            class_name=class_name,
            decorators=decorators,
        )

    def _is_noise_call(self, resolved_name: str) -> bool:
        """Check if a resolved call is noise (built-in attr access, etc.) and should be filtered."""
        parts = resolved_name.split(".")
        # Last part is the actual method/attribute name being called
        last = parts[-1] if parts else ""
        if last in self.SKIP_ATTRS:
            return True
        # Filter calls like self.xxx.append, self.xxx.remove (attribute on attribute)
        # These are not real function-to-function calls
        if len(parts) >= 3 and parts[-1] in self.SKIP_ATTRS:
            return True
        # Filter stdlib/builtin calls that start with known non-project prefixes
        stdlib_prefixes = ("os.", "sys.", "json.", "time.", "copy.", "pickle.",
                           "collections.", "itertools.", "functools.", "enum.",
                           "dataclasses.", "multiprocessing.", "atexit.",
                           "torch.", "numpy.", "xxhash.", "triton.",
                           "flash_attn.", "transformers.", "tqdm.", "safetensors.")
        for prefix in stdlib_prefixes:
            if resolved_name.startswith(prefix) and not self._is_project_internal(resolved_name):
                return True
        return False

    def _is_project_internal(self, name: str) -> bool:
        """Check if a name refers to a symbol within the current project."""
        top = self.module_name.split(".")[0]
        return name.startswith(top + ".")

    def _extract_params(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
        """Extract parameter list with annotations."""
        params = []
        args = node.args

        # Positional-only args (before /)
        for arg in args.posonlyargs:
            p = arg.arg
            if arg.annotation:
                p += f": {self._get_annotation(arg.annotation)}"
            params.append(p)

        for arg in args.args:
            p = arg.arg
            if arg.annotation:
                p += f": {self._get_annotation(arg.annotation)}"
            params.append(p)

        if args.vararg:
            p = f"*{args.vararg.arg}"
            if args.vararg.annotation:
                p += f": {self._get_annotation(args.vararg.annotation)}"
            params.append(p)

        for arg in args.kwonlyargs:
            p = arg.arg
            if arg.annotation:
                p += f": {self._get_annotation(arg.annotation)}"
            params.append(p)

        if args.kwarg:
            p = f"**{args.kwarg.arg}"
            if args.kwarg.annotation:
                p += f": {self._get_annotation(args.kwarg.annotation)}"
            params.append(p)

        # Handle defaults for display
        # defaults covers the last len(defaults) params in posonlyargs + args
        defaults = args.defaults
        kw_defaults = args.kw_defaults
        if defaults:
            n_pos = len(args.posonlyargs) + len(args.args)
            n_simple = n_pos - len(defaults)
            for i, default in enumerate(defaults):
                idx = n_simple + i
                if idx < len(params) and default:
                    params[idx] += f"={self._get_value(default)}"

        for i, default in enumerate(kw_defaults):
            if default and i < len(args.kwonlyargs):
                kw_idx = (len(args.posonlyargs) + len(args.args)
                          + (1 if args.vararg else 0) + i)
                if kw_idx < len(params):
                    params[kw_idx] += f"={self._get_value(default)}"

        return params

    def _get_annotation(self, node: ast.expr | None) -> str:
        if node is None:
            return ""
        try:
            return ast.unparse(node)
        except Exception:
            return "..."

    def _get_value(self, node: ast.expr) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return "..."

    def _get_decorator_name(self, node: ast.expr) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return "..."

    def _resolve_call(self, call_name: str, current_class: str = "") -> str:
        """Try to resolve a call name to a more fully qualified name."""
        parts = call_name.split(".")

        # Check if first part is a known import
        if parts[0] in self.from_imports:
            module, original = self.from_imports[parts[0]]
            if len(parts) == 1:
                # Direct class/function call like Config(...)
                return f"{module}.{original}"
            else:
                return f"{module}.{original}.{'.'.join(parts[1:])}"

        if parts[0] in self.imports:
            module = self.imports[parts[0]]
            return f"{module}.{'.'.join(parts[1:])}" if len(parts) > 1 else module

        # Check if it's a method call on self
        if len(parts) >= 2 and parts[0] == "self":
            method_name = parts[1]
            if current_class:
                # Check if it's a known method of the current class
                for cls in self.classes:
                    if cls.name == current_class:
                        for m in cls.methods:
                            if m.name == method_name:
                                return f"{self.module_name}.{current_class}.{method_name}"
                        break
                # If not a known method, it might be an attribute access - skip
                if method_name not in self._known_method_names:
                    return f"{self.module_name}.{current_class}.{method_name}"
                return f"{self.module_name}.{current_class}.{method_name}"

        # Check if it's a class in this module (constructor call)
        if len(parts) >= 1:
            for cls in self.classes:
                if parts[0] == cls.name:
                    if len(parts) == 1:
                        # Constructor call -> __init__
                        return f"{self.module_name}.{cls.name}.__init__"
                    # Like ClassName.method(...)
                    return f"{self.module_name}.{'.'.join(parts)}"

        # Check if it's a function in this module
        for func in self.functions:
            if parts[0] == func.name:
                return f"{self.module_name}.{func.name}"

        return call_name

    def _extract_source(self, start_line: int, end_line: int | None) -> str:
        if end_line is None:
            end_line = start_line
        lines = self.source_lines[start_line - 1:end_line]
        return "\n".join(lines)

    def _extract_dataclass_fields(self, node: ast.ClassDef) -> list[str]:
        """Extract fields from a dataclass as init_params."""
        fields = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and item.target:
                name = ""
                if isinstance(item.target, ast.Name):
                    name = item.target.id
                if not name:
                    continue
                annotation = ""
                if item.annotation:
                    annotation = self._get_annotation(item.annotation)
                default = ""
                if item.value:
                    default = f"={self._get_value(item.value)}"
                if annotation:
                    fields.append(f"{name}: {annotation}{default}")
                else:
                    fields.append(f"{name}{default}")
        return fields

    def _format_attribute(self, node: ast.Attribute) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            parts.reverse()
            return ".".join(parts)


def _parse_module(args: tuple[str, str]) -> ModuleInfo:
    """Parse a single module (top-level function for ProcessPoolExecutor)."""
    file_path, module_name = args
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            source = f.read()
    except (OSError, UnicodeDecodeError):
        return ModuleInfo(name=module_name, file_path=file_path)
    analyzer = ModuleAnalyzer(module_name, file_path, source)
    return analyzer.analyze()


class CodeAnalyzer:
    """Main analyzer that scans a Python project directory."""

    def __init__(self, root_path: str):
        self.root_path = os.path.abspath(root_path)
        self.project_name = os.path.basename(self.root_path)

    def analyze(self) -> ProjectData:
        """Analyze the entire project and return ProjectData."""
        all_call_edges: list[tuple[str, str]] = []
        all_inheritance: list[tuple[str, str]] = []

        python_files = self._find_python_files()

        # Parallel AST parsing
        with ProcessPoolExecutor() as pool:
            modules = list(pool.map(_parse_module, python_files))

        # Filter out empty modules (no classes and no functions)
        modules = [m for m in modules if m.classes or m.functions]

        # Collect call edges and inheritance
        for module_info in modules:
            for func in module_info.functions:
                for call in func.calls:
                    all_call_edges.append((func.full_name, call))

            for cls in module_info.classes:
                for method in cls.methods:
                    for call in method.calls:
                        all_call_edges.append((method.full_name, call))

                for base in cls.bases:
                    all_inheritance.append((cls.full_name, base))

        # Resolve cross-module call edges
        resolved_edges = self._resolve_edges(all_call_edges, modules)

        # Resolve inheritance edges
        resolved_inheritance = self._resolve_inheritance(all_inheritance, modules)

        # Build hierarchical package tree
        root_package = self._build_package_tree(modules)

        return ProjectData(
            name=self.project_name,
            root_path=self.root_path,
            modules=modules,
            call_edges=resolved_edges,
            class_inheritance=resolved_inheritance,
            root_package=root_package,
        )

    def _find_python_files(self) -> list[tuple[str, str]]:
        results = []
        skip_dirs = {
            "__pycache__", ".git", ".venv", "venv", "env",
            "node_modules", ".tox", ".mypy_cache", ".pytest_cache",
            "dist", "build", "egg-info",
        }

        for dirpath, dirnames, filenames in os.walk(self.root_path):
            dirnames[:] = [
                d for d in dirnames
                if d not in skip_dirs and not d.startswith(".") and not d.endswith(".egg-info")
            ]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                file_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(file_path, self.root_path)
                parts = rel_path.replace(os.sep, "/").replace("/", ".").split(".")
                if parts[-1] == "py":
                    parts = parts[:-1]
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                module_name = ".".join(parts)
                results.append((file_path, module_name))
        return results

    def _resolve_edges(self, edges: list[tuple[str, str]],
                       modules: list[ModuleInfo]) -> list[tuple[str, str]]:
        """Resolve and filter call edges to known project symbols at function granularity."""
        known_funcs: set[str] = set()
        known_classes: dict[str, str] = {}
        class_short_to_full: dict[str, list[str]] = {}

        for mod in modules:
            for func in mod.functions:
                known_funcs.add(func.full_name)
            for cls in mod.classes:
                class_short_to_full.setdefault(cls.name, []).append(cls.full_name)
                has_init = any(m.name == "__init__" for m in cls.methods)
                if has_init:
                    known_classes[cls.full_name] = f"{cls.full_name}.__init__"
                else:
                    has_post_init = any(m.name == "__post_init__" for m in cls.methods)
                    if has_post_init:
                        known_classes[cls.full_name] = f"{cls.full_name}.__post_init__"
                    else:
                        known_classes[cls.full_name] = cls.full_name
                        known_funcs.add(cls.full_name)
                for method in cls.methods:
                    known_funcs.add(method.full_name)

        # Build suffix index: short_name -> full_name (for O(1) suffix lookup)
        suffix_index: dict[str, str] = {}
        for full in known_funcs:
            short = full.rsplit(".", 1)[-1]
            # Only index if not ambiguous; first wins (same as old behavior)
            if short not in suffix_index:
                suffix_index[short] = full

        # Build module prefix set for O(1) module prefix matching
        module_prefixes: dict[str, str] = {}
        for mod in modules:
            # Map callee -> mod.name.callee for quick lookup
            # We'll use this differently - just collect module names
            pass
        # For module prefix matching, pre-build: for each callee, check if mod.name.callee exists
        # This is hard to pre-index since callee varies. Keep as O(M) loop but M is modules count.

        project_top = modules[0].name.split(".")[0] if modules else ""

        resolved = []
        seen = set()

        for caller, callee in edges:
            key = (caller, callee)
            if key in seen:
                continue

            if callee in known_funcs:
                resolved.append((caller, callee))
                seen.add(key)
                continue

            if callee in known_classes:
                target = known_classes[callee]
                new_key = (caller, target)
                if new_key not in seen:
                    resolved.append((caller, target))
                    seen.add(new_key)
                continue

            if callee.endswith(".__init__"):
                class_name = callee[:-9]
                if class_name in known_classes:
                    target = known_classes[class_name]
                    new_key = (caller, target)
                    if new_key not in seen:
                        resolved.append((caller, target))
                        seen.add(new_key)
                    continue

            parts = callee.split(".")
            matched = False
            for i in range(len(parts)):
                candidate = ".".join(parts[i:])
                if candidate in class_short_to_full:
                    full_name = class_short_to_full[candidate][0]
                    best_target = known_classes.get(full_name, full_name)
                    if len(parts) > i + 1:
                        method_part = ".".join(parts[i+1:])
                        method_full = f"{full_name}.{method_part}"
                        if method_full in known_funcs:
                            new_key = (caller, method_full)
                            if new_key not in seen:
                                resolved.append((caller, method_full))
                                seen.add(new_key)
                            matched = True
                            break
                    if not matched:
                        new_key = (caller, best_target)
                        if new_key not in seen:
                            resolved.append((caller, best_target))
                            seen.add(new_key)
                        matched = True
                        break
            if matched:
                continue

            # Module prefix matching (O(M) where M = module count)
            for mod in modules:
                full_candidate = f"{mod.name}.{callee}"
                if full_candidate in known_funcs:
                    new_key = (caller, full_candidate)
                    if new_key not in seen:
                        resolved.append((caller, full_candidate))
                        seen.add(new_key)
                    matched = True
                    break
                if full_candidate in known_classes:
                    init_name = known_classes[full_candidate]
                    new_key = (caller, init_name)
                    if new_key not in seen:
                        resolved.append((caller, init_name))
                        seen.add(new_key)
                    matched = True
                    break
            if matched:
                continue

            # Suffix index lookup (O(1) instead of O(N) endswith loop)
            if project_top and callee.startswith(project_top + "."):
                short = callee.rsplit(".", 1)[-1]
                known = suffix_index.get(short)
                if known:
                    new_key = (caller, known)
                    if new_key not in seen:
                        resolved.append((caller, known))
                        seen.add(new_key)
                    continue

            # Project root prefix stripping: from_imports may produce FQNs
            # with an absolute package prefix (e.g. "nanovllm.utils.context.get_context")
            # while known_funcs uses relative paths (e.g. "utils.context.get_context").
            # Try stripping leading dotted prefixes until we match a known symbol.
            if not matched:
                stripped = callee
                while "." in stripped:
                    stripped = stripped.split(".", 1)[1]
                    if stripped in known_funcs:
                        new_key = (caller, stripped)
                        if new_key not in seen:
                            resolved.append((caller, stripped))
                            seen.add(new_key)
                        matched = True
                        break
                    if stripped in known_classes:
                        target = known_classes[stripped]
                        new_key = (caller, target)
                        if new_key not in seen:
                            resolved.append((caller, target))
                            seen.add(new_key)
                        matched = True
                        break

        return resolved

    def _build_package_tree(self, modules: list[ModuleInfo]) -> PackageInfo:
        """Build a hierarchical package tree from module dotted names."""
        if not modules:
            return PackageInfo(name=self.project_name, full_name=self.project_name)

        # Collect all unique package prefixes
        package_names: set[str] = set()
        for mod in modules:
            parts = mod.name.split(".")
            for i in range(1, len(parts)):
                package_names.add(".".join(parts[:i]))

        # Create PackageInfo for each package
        pkg_map: dict[str, PackageInfo] = {}
        for pname in package_names:
            pkg_map[pname] = PackageInfo(name=pname, full_name=pname)

        # Assign modules and sub-packages to their parent packages
        root_candidates: set[str] = set(package_names)
        for pname in package_names:
            parts = pname.split(".")
            if len(parts) > 1:
                parent_name = ".".join(parts[:-1])
                if parent_name in pkg_map:
                    root_candidates.discard(pname)
                    pkg_map[parent_name].children.append(pkg_map[pname])

        # Assign modules to their direct parent package.
        # An __init__.py module shares its dotted name with its package
        # (the "__init__" segment is stripped in _find_python_files), so it
        # must attach to that package itself — not to its grandparent, which
        # is what parts[:-1] would otherwise yield.
        for mod in modules:
            if mod.name in pkg_map:
                pkg_map[mod.name].children.append(mod)
                continue
            parts = mod.name.split(".")
            if len(parts) > 1:
                parent_name = ".".join(parts[:-1])
                if parent_name in pkg_map:
                    pkg_map[parent_name].children.append(mod)
            # Modules with no dots are top-level (belong to root)

        # Find root package(s) — those not added as children of any other
        if not root_candidates:
            # All packages are nested; find the shallowest
            shallowest = min(package_names, key=lambda n: n.count("."))
            root_candidates = {shallowest}

        # Build root: if single root candidate, use it; else wrap in a virtual root
        if len(root_candidates) == 1:
            root = pkg_map[root_candidates.pop()]
        elif root_candidates:
            root = PackageInfo(name=self.project_name, full_name=self.project_name)
            for rc in sorted(root_candidates):
                root.children.append(pkg_map[rc])
        else:
            root = PackageInfo(name=self.project_name, full_name=self.project_name)

        # Add top-level modules (no parent package) to root.
        # Skip __init__ modules already attached to their own package above.
        for mod in modules:
            parts = mod.name.split(".")
            if len(parts) <= 1 and mod.name not in pkg_map:
                root.children.append(mod)

        # Collapse single-child chains (package with 1 child that is a package)
        root = self._collapse_single_chains(root)

        # Mark leaf packages
        self._mark_leaf_packages(root)

        # Sort children: packages first (alphabetically), then modules (alphabetically)
        self._sort_children(root)

        return root

    def _collapse_single_chains(self, pkg: PackageInfo) -> PackageInfo:
        """Collapse packages that have exactly 1 child which is also a package."""
        # First recurse into children
        new_children = []
        for child in pkg.children:
            if isinstance(child, PackageInfo):
                child = self._collapse_single_chains(child)
                new_children.append(child)
            else:
                new_children.append(child)
        pkg.children = new_children

        # Then collapse: if this package has exactly 1 PackageInfo child and no modules
        if len(pkg.children) == 1 and isinstance(pkg.children[0], PackageInfo):
            return pkg.children[0]
        return pkg

    def _mark_leaf_packages(self, pkg: PackageInfo) -> None:
        """Mark packages that only contain modules (no sub-packages)."""
        has_sub_pkg = any(isinstance(c, PackageInfo) for c in pkg.children)
        pkg.is_leaf = not has_sub_pkg
        for child in pkg.children:
            if isinstance(child, PackageInfo):
                self._mark_leaf_packages(child)

    def _sort_children(self, pkg: PackageInfo) -> None:
        """Sort children: packages first (alphabetically), then modules."""
        pkgs = sorted(
            [c for c in pkg.children if isinstance(c, PackageInfo)],
            key=lambda c: c.name
        )
        mods = sorted(
            [c for c in pkg.children if isinstance(c, ModuleInfo)],
            key=lambda c: c.name
        )
        pkg.children = pkgs + mods
        for child in pkg.children:
            if isinstance(child, PackageInfo):
                self._sort_children(child)

    def _resolve_inheritance(self, edges: list[tuple[str, str]],
                             modules: list[ModuleInfo]) -> list[tuple[str, str]]:
        """Resolve inheritance edges to full class names within the project."""
        class_short_to_full: dict[str, list[str]] = {}
        for mod in modules:
            for cls in mod.classes:
                class_short_to_full.setdefault(cls.name, []).append(cls.full_name)

        resolved = []
        seen = set()
        for child, parent in edges:
            key = (child, parent)
            if key in seen:
                continue
            # Try to resolve parent short name to full name
            if parent in class_short_to_full:
                resolved.append((child, class_short_to_full[parent][0]))
            else:
                resolved.append((child, parent))
            seen.add(key)

        return resolved

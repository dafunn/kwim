"""Tree-sitter structure + definition extraction (Python).

Produces, per file: the module qualified name, definitions (functions/classes/
methods with signatures + docstring summaries), imports (alias -> target module/
symbol), and call-sites (enclosing-def QN -> raw callee name). Resolution of
call-sites to target QNs happens in resolve.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspython

_PY = Language(tspython.language())
_PARSER = Parser(_PY)


@dataclass
class Defn:
    kind: str            # "function" | "method" | "class"
    qn: str              # qualified name: module.Class.method | module.func
    name: str            # simple name
    signature: str = ""
    summary: str = ""
    start_line: int = 0
    end_line: int = 0
    methods: list[str] = field(default_factory=list)  # class only


@dataclass
class ImportRec:
    alias: str           # name bound in this file
    target: str          # dotted module path, or module.symbol
    external: bool       # True if it resolves outside the indexed repo set (decided later)


@dataclass
class CallSite:
    caller_qn: str       # enclosing def QN, or the module QN for top-level calls
    callee_name: str     # raw: "foo", "obj.method", "mod.func"
    line: int


@dataclass
class ParsedFile:
    path: str
    module_qn: str
    defns: list[Defn] = field(default_factory=list)
    imports: list[ImportRec] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)


def module_qn_for(rel_path: str) -> str:
    """app/stores/falkor.py -> app.stores.falkor ; pkg/__init__.py -> pkg."""
    p = rel_path
    if p.endswith(".py"):
        p = p[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _child(node: Node, field_name: str) -> Node | None:
    return node.child_by_field_name(field_name)


def _docstring_summary(body: Node, src: bytes) -> str:
    """First line of the def's docstring, if any."""
    for child in body.named_children:
        if child.type == "expression_statement" and child.named_child_count:
            inner = child.named_children[0]
            if inner.type == "string":
                raw = _text(inner, src).strip().strip("'\"")
                return raw.splitlines()[0].strip() if raw else ""
        break  # docstring must be the first statement
    return ""


def _signature(defn_node: Node, name: str, src: bytes) -> str:
    params = _child(defn_node, "parameters")
    ret = _child(defn_node, "return_type")
    sig = name + (_text(params, src) if params else "()")
    if ret:
        sig += " -> " + _text(ret, src)
    return sig


def _unwrap(node: Node) -> Node:
    """decorated_definition wraps the real function/class definition."""
    if node.type == "decorated_definition":
        d = _child(node, "definition")
        return d or node
    return node


def parse_python(rel_path: str, source: bytes) -> ParsedFile:
    module_qn = module_qn_for(rel_path)
    tree = _PARSER.parse(source)
    pf = ParsedFile(path=rel_path, module_qn=module_qn)

    _collect_imports(tree.root_node, source, module_qn, pf)
    # Walk top-level statements, tracking the qualified-name scope.
    _walk(tree.root_node, source, scope_qn=module_qn, enclosing_def=module_qn,
          pf=pf, class_ctx=None)
    return pf


def _collect_imports(root: Node, src: bytes, module_qn: str, pf: ParsedFile) -> None:
    pkg = module_qn.rsplit(".", 1)[0] if "." in module_qn else ""
    for node in _iter(root):
        if node.type == "import_statement":
            for ch in node.named_children:
                if ch.type == "dotted_name":
                    dotted = _text(ch, src)
                    pf.imports.append(ImportRec(alias=dotted.split(".")[0], target=dotted, external=False))
                elif ch.type == "aliased_import":
                    name = _child(ch, "name")
                    alias = _child(ch, "alias")
                    if name and alias:
                        pf.imports.append(ImportRec(alias=_text(alias, src), target=_text(name, src), external=False))
        elif node.type == "import_from_statement":
            mod_node = _child(node, "module_name")
            base = _text(mod_node, src) if mod_node else ""
            # relative import: leading dots -> resolve against current package
            if base.startswith(".") or (mod_node and mod_node.type == "relative_import"):
                rel = base.lstrip(".")
                base = (pkg + ("." + rel if rel else "")) if pkg else rel
            for ch in node.named_children:
                if ch == mod_node:
                    continue
                if ch.type == "dotted_name":
                    sym = _text(ch, src)
                    pf.imports.append(ImportRec(alias=sym.split(".")[-1], target=f"{base}.{sym}" if base else sym, external=False))
                elif ch.type == "aliased_import":
                    name = _child(ch, "name")
                    alias = _child(ch, "alias")
                    if name and alias:
                        sym = _text(name, src)
                        pf.imports.append(ImportRec(alias=_text(alias, src), target=f"{base}.{sym}" if base else sym, external=False))


def _walk(node: Node, src: bytes, scope_qn: str, enclosing_def: str, pf: ParsedFile,
          class_ctx: Defn | None) -> None:
    for child in node.named_children:
        real = _unwrap(child)
        if real.type == "function_definition":
            name_node = _child(real, "name")
            if not name_node:
                continue
            name = _text(name_node, src)
            qn = f"{scope_qn}.{name}"
            kind = "method" if class_ctx is not None else "function"
            body = _child(real, "body")
            defn = Defn(
                kind=kind, qn=qn, name=name,
                signature=_signature(real, name, src),
                summary=_docstring_summary(body, src) if body else "",
                start_line=real.start_point[0] + 1, end_line=real.end_point[0] + 1,
            )
            pf.defns.append(defn)
            if class_ctx is not None:
                class_ctx.methods.append(name)
            # descend into the function body: nested defs + call-sites enclosed by qn
            if body:
                _walk(body, src, scope_qn=qn, enclosing_def=qn, pf=pf, class_ctx=None)
        elif real.type == "class_definition":
            name_node = _child(real, "name")
            if not name_node:
                continue
            name = _text(name_node, src)
            qn = f"{scope_qn}.{name}"
            defn = Defn(
                kind="class", qn=qn, name=name,
                signature=name, summary="",
                start_line=real.start_point[0] + 1, end_line=real.end_point[0] + 1,
            )
            pf.defns.append(defn)
            body = _child(real, "body")
            if body:
                _walk(body, src, scope_qn=qn, enclosing_def=enclosing_def, pf=pf, class_ctx=defn)
        else:
            # not a def: scan for call-sites attributed to the enclosing def, then recurse
            _scan_calls(child, src, enclosing_def, pf)
            _walk(child, src, scope_qn=scope_qn, enclosing_def=enclosing_def, pf=pf, class_ctx=class_ctx)


def _scan_calls(node: Node, src: bytes, enclosing_def: str, pf: ParsedFile) -> None:
    """Record call-sites in `node`, but do not descend into nested def bodies
    (those are handled by _walk with their own enclosing_def)."""
    if node.type in ("function_definition", "class_definition", "decorated_definition"):
        return
    if node.type == "call":
        fn = _child(node, "function")
        if fn is not None:
            callee = _callee_name(fn, src)
            if callee:
                pf.calls.append(CallSite(caller_qn=enclosing_def, callee_name=callee, line=node.start_point[0] + 1))
    for child in node.named_children:
        _scan_calls(child, src, enclosing_def, pf)


def _callee_name(fn: Node, src: bytes) -> str:
    """Raw textual callee: identifier 'foo' or attribute 'obj.method'/'mod.func'."""
    if fn.type == "identifier":
        return _text(fn, src)
    if fn.type == "attribute":
        return _text(fn, src)  # e.g. "self.foo", "mod.func", "a.b.c"
    return ""


def _iter(node: Node):
    """Pre-order iteration over all named descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.named_children))

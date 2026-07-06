"""Confidence-scored call resolution.

Ported from DeusData/codebase-memory-mcp registry.c (MIT). A Registry indexes all
definitions by qualified name + simple name; resolve() maps a raw call-site callee
to a target QN via a cascade, tagging each result with a confidence + strategy so
downstream ranking and the W-layer can filter low-trust edges.

Confidence constants are tuned for the resolution cascade below.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from .parse import ImportRec, ParsedFile

# The confidence cascade is configuration (codegraph.resolution.* in kwim.defaults.yaml,
# env-overridable). These module names are kept as the call-site/test contract.
CONF_IMPORT_MAP = settings.cg_conf_import_map
CONF_IMPORT_MAP_SUFFIX = settings.cg_conf_import_map_suffix
CONF_SAME_CLASS = settings.cg_conf_same_class      # self.m / cls.m within the caller's own class
CONF_SAME_MODULE = settings.cg_conf_same_module
CONF_QUALIFIED_SUFFIX = settings.cg_conf_qualified_suffix
CONF_UNIQUE_NAME = settings.cg_conf_unique_name
CONF_SUFFIX_MATCH = settings.cg_conf_suffix_match
CONF_FUZZY_SINGLE = settings.cg_conf_fuzzy_single
CONF_FUZZY_MULTI = settings.cg_conf_fuzzy_multi

# Above this many same-name candidates, treat as unresolvably ambiguous (skip).
REG_MAX_CANDIDATES = settings.cg_reg_max_candidates


@dataclass
class Resolution:
    qn: str | None
    strategy: str
    confidence: float
    candidates: int = 1

    @property
    def resolved(self) -> bool:
        return bool(self.qn)


_EMPTY = Resolution(qn=None, strategy="unresolved", confidence=0.0, candidates=0)


class Registry:
    """All definition QNs across the indexed repo set, with a simple-name index."""

    def __init__(self) -> None:
        self._exact: set[str] = set()
        self._by_name: dict[str, list[str]] = {}

    def add(self, qn: str) -> None:
        if qn in self._exact:
            return
        self._exact.add(qn)
        simple = qn.rsplit(".", 1)[-1]
        self._by_name.setdefault(simple, []).append(qn)

    def has(self, qn: str) -> bool:
        return qn in self._exact

    def by_name(self, simple: str) -> list[str]:
        return self._by_name.get(simple, [])


def build_registry(parsed: list[ParsedFile]) -> Registry:
    reg = Registry()
    for pf in parsed:
        for d in pf.defns:
            reg.add(d.qn)
    return reg


def _import_map(pf: ParsedFile) -> dict[str, str]:
    """alias -> target (module path or module.symbol)."""
    return {imp.alias: imp.target for imp in pf.imports}


def resolve_call(reg: Registry, callee: str, module_qn: str, imap: dict[str, str],
                 caller_qn: str | None = None) -> Resolution:
    """The cascade. `callee` is the raw textual name (foo | obj.method | mod.func).
    `caller_qn` (module.Class.method) enables same-class resolution of self.X/cls.X."""
    # self.method / cls.method -> treat suffix as the method name for resolution.
    prefix, _, suffix = callee.partition(".")
    if "." not in callee:
        prefix, suffix = callee, ""

    # Strategy 0: same-class. `self.m` / `cls.m` from inside module.Class.method
    # resolves to module.Class.m when that exists - beats the ambiguous suffix tier.
    if prefix in ("self", "cls") and suffix and caller_qn and "." in caller_qn:
        enclosing_class = caller_qn.rsplit(".", 1)[0]
        cand = f"{enclosing_class}.{suffix.rsplit('.', 1)[-1]}"
        if reg.has(cand):
            return Resolution(cand, "same_class", CONF_SAME_CLASS)

    # Strategy 1: import map. prefix is an imported alias -> resolved target.
    if prefix in imap:
        resolved = imap[prefix]
        cand = f"{resolved}.{suffix}" if suffix else resolved
        if reg.has(cand):
            return Resolution(cand, "import_map", CONF_IMPORT_MAP)
        # import_map_suffix fallback: a known QN ending with the suffix under resolved.
        if suffix:
            for qn in reg.by_name(suffix.rsplit(".", 1)[-1]):
                if qn.startswith(resolved + ".") and qn.endswith("." + suffix):
                    return Resolution(qn, "import_map_suffix", CONF_IMPORT_MAP_SUFFIX)

    # Strategy 2: same module. module_qn.callee (handles bare calls + self.method
    # when the method is defined in a class in this module is covered by name lookup).
    cand = f"{module_qn}.{callee}"
    if reg.has(cand):
        return Resolution(cand, "same_module", CONF_SAME_MODULE)
    if suffix:
        cand = f"{module_qn}.{suffix}"
        if reg.has(cand):
            return Resolution(cand, "same_module", CONF_SAME_MODULE)

    # Strategy 3+: name lookup by simple name.
    simple = (suffix or prefix).rsplit(".", 1)[-1]
    candidates = reg.by_name(simple)
    n = len(candidates)
    if n == 0 or n > REG_MAX_CANDIDATES:
        return _EMPTY
    if n == 1:
        return Resolution(candidates[0], "unique_name", CONF_UNIQUE_NAME)
    # 3.5: qualified-suffix disambiguation among multiple same-name candidates.
    if suffix:
        tail = "." + suffix
        exact_tail = [q for q in candidates if q.endswith(tail)]
        if len(exact_tail) == 1:
            return Resolution(exact_tail[0], "qualified_suffix", CONF_QUALIFIED_SUFFIX, n)
    # 4: suffix-match - pick the import-distance-nearest; approximate by shortest QN.
    best = min(candidates, key=len)
    return Resolution(best, "suffix_match", CONF_SUFFIX_MATCH, n)


def resolve_file(reg: Registry, pf: ParsedFile) -> list[tuple[str, str, Resolution]]:
    """Resolve all call-sites in a file. Returns (caller_qn, callee_raw, Resolution)
    for resolved calls only (callers that are themselves known defs)."""
    imap = _import_map(pf)
    known_defs = {d.qn for d in pf.defns}
    out: list[tuple[str, str, Resolution]] = []
    for call in pf.calls:
        # only emit edges from a real Function/Method node (skip module-level glue)
        if call.caller_qn not in known_defs:
            continue
        res = resolve_call(reg, call.callee_name, pf.module_qn, imap, caller_qn=call.caller_qn)
        if res.resolved:
            out.append((call.caller_qn, call.callee_name, res))
    return out

"""Code-graph parse + resolution cascade tests.

Validates the confidence cascade resolves to the expected target + strategy on
controlled fixtures, and that ambiguous calls land in the low-confidence tier.
"""
from app.codegraph import parse, resolve


def _parse(files: dict[str, str]) -> list[parse.ParsedFile]:
    return [parse.parse_python(path, src.encode()) for path, src in files.items()]


# --- Fixture: a tiny two-module repo --------------------------------------
FILES = {
    "pkg/util.py": (
        "def helper(x):\n"
        "    '''Does a thing.'''\n"
        "    return x\n"
    ),
    "pkg/svc.py": (
        "from pkg.util import helper\n"
        "import pkg.util as u\n"
        "\n"
        "def free_fn(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        free_fn(1, 2)\n"          # same_module -> pkg.svc.free_fn
        "        helper(3)\n"              # import_map -> pkg.util.helper
        "        u.helper(4)\n"            # import_map via alias -> pkg.util.helper
        "        self.step()\n"           # same_class -> pkg.svc.Worker.step\n"
        "\n"
        "    def step(self):\n"
        "        return 1\n"
    ),
}


def test_parse_structure():
    parsed = {pf.path: pf for pf in _parse(FILES)}
    svc = parsed["pkg/svc.py"]
    assert svc.module_qn == "pkg.svc"
    qns = {d.qn for d in svc.defns}
    assert qns == {"pkg.svc.free_fn", "pkg.svc.Worker",
                   "pkg.svc.Worker.run", "pkg.svc.Worker.step"}
    util = parsed["pkg/util.py"]
    helper = next(d for d in util.defns if d.name == "helper")
    assert helper.summary == "Does a thing."
    assert helper.signature == "helper(x)"


def test_resolution_strategies():
    parsed = _parse(FILES)
    reg = resolve.build_registry(parsed)
    svc = next(pf for pf in parsed if pf.path == "pkg/svc.py")
    edges = {(raw): res for _caller, raw, res in resolve.resolve_file(reg, svc)}

    assert edges["free_fn"].qn == "pkg.svc.free_fn"
    assert edges["free_fn"].strategy == "same_module"

    assert edges["helper"].qn == "pkg.util.helper"
    assert edges["helper"].strategy == "import_map"

    assert edges["u.helper"].qn == "pkg.util.helper"
    assert edges["u.helper"].strategy == "import_map"

    assert edges["self.step"].qn == "pkg.svc.Worker.step"
    assert edges["self.step"].strategy == "same_class"
    assert edges["self.step"].confidence == resolve.CONF_SAME_CLASS


def test_ambiguous_is_low_confidence():
    # Two classes define method `m`; a bare `obj.m()` is unresolvably ambiguous and
    # must land in the low-confidence suffix tier (never high-confidence).
    files = {
        "a.py": "class A:\n    def m(self):\n        return 1\n",
        "b.py": "class B:\n    def m(self):\n        return 2\n",
        "c.py": (
            "def caller(obj):\n"
            "    obj.m()\n"
        ),
    }
    parsed = _parse(files)
    reg = resolve.build_registry(parsed)
    c = next(pf for pf in parsed if pf.path == "c.py")
    edges = resolve.resolve_file(reg, c)
    # `obj.m` has 2 candidates; resolved (suffix) but below the high-conf bar.
    res = next(r for _a, raw, r in edges if raw == "obj.m")
    assert res.strategy == "suffix_match"
    assert res.confidence <= 0.55
    assert res.candidates == 2


def test_unresolved_external_not_emitted():
    files = {
        "x.py": (
            "import os\n"
            "def f():\n"
            "    os.getcwd()\n"           # external - must not resolve to an internal def
        ),
    }
    parsed = _parse(files)
    reg = resolve.build_registry(parsed)
    edges = resolve.resolve_file(reg, parsed[0])
    assert [r for _a, raw, r in edges if "getcwd" in raw] == []

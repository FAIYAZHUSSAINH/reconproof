"""The architecture, enforced by a test.

Two claims in the README are checkable mechanically, so they are checked here
rather than asserted in prose:

1. `verify.py` imports nothing from the matcher or the LLM layer.
2. No float literal appears in any module that touches money.

A reviewer who wants to know whether the trust boundary is real can run this
file instead of taking my word for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "reconproof"

# What the verifier is allowed to know about.
VERIFIER_ALLOWED_LOCAL_IMPORTS = {"models", "money"}

# Modules where a float would be a money bug. `confidence.py` is excluded on
# purpose: a confidence score is not money and is a float by design.
MONEY_MODULES = (
    "money.py",
    "models.py",
    "generate.py",
    "ingest.py",
    "candidates.py",
    "proof.py",
    "verify.py",
    "pipeline.py",
)


def local_imports(path: Path) -> set[str]:
    """Every module inside this package that `path` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                found.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("reconproof"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("reconproof."):
                    found.add(alias.name.split(".")[1])
    return found


class TestVerifierIsolation:
    def test_the_verifier_imports_only_models_and_money(self):
        imports = local_imports(SOURCE / "verify.py")
        assert imports <= VERIFIER_ALLOWED_LOCAL_IMPORTS, (
            f"verify.py imports {sorted(imports - VERIFIER_ALLOWED_LOCAL_IMPORTS)}. "
            "The verifier must not share code with the thing it checks."
        )

    @pytest.mark.parametrize(
        "forbidden", ["candidates", "proof", "adjudicate", "pipeline", "report", "generate"]
    )
    def test_the_verifier_cannot_reach_the_matcher(self, forbidden):
        assert forbidden not in local_imports(SOURCE / "verify.py")

    def test_the_verifier_does_not_import_an_llm_client(self):
        text = (SOURCE / "verify.py").read_text(encoding="utf-8")
        for needle in ("groq", "openai", "requests", "httpx", "urllib"):
            assert needle not in text.lower(), f"verify.py mentions {needle}"

    def test_models_and_money_do_not_import_anything_else_local(self):
        """The verifier's dependencies must stay leaf modules.

        Otherwise the isolation above could be defeated one hop away.
        """
        assert local_imports(SOURCE / "money.py") == set()
        assert local_imports(SOURCE / "models.py") <= {"money"}


# The only two fields in these modules that are floats on purpose. Neither is
# money: one is a score, one is a stopwatch. They are listed by name so that
# the exemption is visible in the test rather than implied by its absence.
NON_MONEY_FLOAT_FIELDS = {"confidence", "elapsed_seconds"}

# `/` is also pathlib's join operator, and these modules read and write files.
# A name containing one of these is a directory, not an amount.
PATH_NAME_HINTS = ("dir", "path", "root")


def _assigned_name(node: ast.AST) -> str | None:
    """The single name an assignment binds, if it binds exactly one."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        if isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
    return None


def _looks_like_a_path(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    name = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _looks_like_a_path(node.left) or _looks_like_a_path(node.right)
    return bool(name) and any(hint in name.lower() for hint in PATH_NAME_HINTS)


class TestNoFloatsInMoneyPaths:
    @pytest.mark.parametrize("filename", MONEY_MODULES)
    def test_no_float_literals(self, filename):
        tree = ast.parse((SOURCE / filename).read_text(encoding="utf-8"))

        exempt: set[int] = set()
        for node in ast.walk(tree):
            if _assigned_name(node) in NON_MONEY_FLOAT_FIELDS:
                exempt.update(id(child) for child in ast.walk(node.value))

        floats = [
            (node.lineno, node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and id(node) not in exempt
        ]
        assert not floats, f"{filename} contains float literals: {floats}"

    @pytest.mark.parametrize("filename", MONEY_MODULES)
    def test_no_rounding_helpers(self, filename):
        """`round()` and `/` on money are how paise go missing.

        Division is checked as true division only; `//` is how the integer
        rounding helpers in money.py are written. Path joins are exempt - see
        PATH_NAME_HINTS - because pathlib overloads the same operator.
        """
        source = (SOURCE / filename).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "round", f"{filename} calls round()"
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if _looks_like_a_path(node.left) or _looks_like_a_path(node.right):
                    continue
                raise AssertionError(
                    f"{filename} uses true division on line {node.lineno}: "
                    f"{ast.unparse(node)}"
                )


class TestLlmLayerIsOptional:
    def test_no_module_imports_groq_at_module_level(self):
        """Importing the package must not require the groq client.

        A judge with no API key and no groq install still gets a full run, so
        the import happens inside the adjudicator's constructor.
        """
        for path in sorted(SOURCE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # module level only
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    text = ast.dump(node)
                    assert "groq" not in text.lower(), f"{path.name} imports groq at import time"

    def test_the_pipeline_runs_without_an_adjudicator(self, store):
        from reconproof.pipeline import run_pipeline

        result = run_pipeline(store, adjudicator=None)
        assert result.accepted
        assert result.params["llm"] is False

# tests/test_ci_extra.py
"""Keep the ``[ci]`` extra honest.

.github/workflows/tests.yml installs only ``.[ci]``. If a test starts importing
a package that is not in that extra, CI fails at import time with a
``ModuleNotFoundError`` that looks like a broken test rather than a missing
dependency. This check turns that into an explicit, local failure.
"""
import ast
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _declared_ci_packages() -> set[str]:
    """Top-level distribution names declared by the ``[ci]`` extra."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        pytest.skip("tomllib requires Python 3.11+")

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)

    requirements = pyproject["project"]["optional-dependencies"]["ci"]
    names: set[str] = set()
    for req in requirements:
        # Strip extras ("a[b]"), version specifiers, and environment markers.
        base = req.split(";", 1)[0].strip()
        base = base.split("[", 1)[0]
        for sep in (">=", "<=", "==", "~=", "!=", ">", "<"):
            base = base.split(sep, 1)[0]
        if base:
            names.add(base.strip().lower().replace("_", "-"))
    return names


def _local_module_names() -> set[str]:
    """First-party package/module names that resolve without installation."""
    names = {p.name for p in REPO_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")}
    names |= {p.stem for p in REPO_ROOT.glob("*.py")}
    return names


def _test_module_imports() -> set[str]:
    """Top-level third-party modules imported anywhere under tests/."""
    stdlib = set(sys.stdlib_module_names)
    local = _local_module_names()
    found: set[str] = set()

    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])

    return {
        name
        for name in found
        if name not in stdlib and name not in local and name != "tests"
    }


def test_ci_extra_is_declared():
    """Sanity check that we are reading a populated extra."""
    assert _declared_ci_packages(), "[ci] extra should not be empty"


def test_test_imports_are_covered_by_ci_extra():
    """Every third-party module imported by tests must be installable via [ci].

    Distributions and import names differ (``yaml`` vs ``PyYAML``,
    ``sklearn`` vs ``scikit-learn``), so a few well-known aliases are mapped
    rather than guessed at generically.
    """
    aliases = {
        "yaml": "pyyaml",
        "sklearn": "scikit-learn",
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "dotenv": "python-dotenv",
        "jwt": "pyjwt",
        "PIL": "pillow",
        "serial": "pyserial",
        "OpenSSL": "pyopenssl",
    }

    declared = _declared_ci_packages()
    # The extra pulls in [agents]/[api]/[dev], which transitively bring these.
    declared |= {
        "crewai", "langchain", "langchain-core", "langgraph", "litellm",
        "anthropic", "pandas-ta", "scrapling", "crewai-tools", "instructor",
        "fastapi", "uvicorn", "pytest", "pytest-asyncio", "sqlalchemy",
        "pydantic", "pydantic-settings", "numpy", "pandas", "yfinance",
        "loguru", "alembic", "stockstats", "typer", "rich", "questionary",
        "tqdm", "parsel", "pytz", "python-dotenv", "httpx", "requests",
        "prompt-toolkit",
    }

    missing = set()
    for module in _test_module_imports():
        dist = aliases.get(module, module.lower().replace("_", "-"))
        if dist not in declared:
            missing.add(f"{module} (expected dist '{dist}')")

    assert not missing, (
        "tests/ imports packages not provided by the [ci] extra. CI installs "
        f"only .[ci] and would fail with ModuleNotFoundError. Add: {sorted(missing)}"
    )
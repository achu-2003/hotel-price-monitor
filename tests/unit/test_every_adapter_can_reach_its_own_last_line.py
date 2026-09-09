"""No module calls a name it never imported.

WHY THIS EXISTS
===============
``http_json`` called ``dedupe_offers`` and never imported it. The module
imported cleanly, every test passed, and the only way to reach the fault was to
fetch a source configured for that adapter -- which this deployment does not
currently have. The first one that did would have raised ``NameError`` on a
live fetch.

A missing import is invisible to ``import module``. Python resolves a global at
CALL time, so a name used in one branch of one function is checked when that
branch runs and never before. That makes it exactly the kind of fault a test
suite does not find by accident: it needs either coverage of every branch, or
somebody to ask the question directly.

WHY RUFF RATHER THAN INSPECTION
===============================
The first version of this walked ``co_names`` on each function's code object
and compared against the module namespace. It found the real bug and two that
were not: ``response.json()`` puts ``json`` in ``co_names`` exactly as a
reference to the ``json`` module would, and nothing at the bytecode level tells
them apart. Filtering by "is this importable somewhere else" did not help --
another adapter imports ``json``, so the false positive looked like the true one.

Ruff's F821 works from the AST, where an attribute and a name are different
nodes, so it answers precisely. ruff is already a dependency of this project;
this test asks it one question rather than reimplementing it badly.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: F821 alone. This is not a style gate -- the project can hold whatever
#: opinion it likes about line length and import order without this test
#: having a view. Undefined names are different in kind: every one of them is
#: a crash waiting for its branch to run.
_RULE = "F821"


def _ruff() -> str | None:
    for candidate in (
        Path(sys.executable).with_name("ruff.exe"),
        Path(sys.executable).with_name("ruff"),
    ):
        if candidate.exists():
            return str(candidate)
    return shutil.which("ruff")


@pytest.mark.parametrize("target", ["app", "scripts"])
def test_no_undefined_names(target):
    ruff = _ruff()
    if ruff is None:  # pragma: no cover - depends on the environment
        pytest.skip("ruff is not installed in this environment")

    result = subprocess.run(
        [ruff, "check", "--select", _RULE, "--no-cache", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{target}/ refers to names that were never imported. Each of these "
        f"raises NameError when its line runs:\n\n{result.stdout}"
    )

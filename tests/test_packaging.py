"""What a person gets from `pip install`: the right name, one version, the files the
GUI needs and an install hint that works. The CI job `package` installs the built
wheel into a clean environment and checks the same from outside the repository."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest

import dqp
from dqp.cli import main

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
DIST_NAME = re.search(r'^name = "([^"]+)"', PYPROJECT, re.M).group(1)


def test_version_has_one_source():
    assert 'dynamic = ["version"]' in PYPROJECT
    assert 'version = { attr = "dqp.__version__" }' in PYPROJECT
    assert re.fullmatch(r"\d+\.\d+\.\d+", dqp.__version__)


def test_dqp_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"{DIST_NAME} {dqp.__version__}"


def test_every_install_hint_names_the_real_package():
    """`pip install dqp` installs somebody else's package; `dqp[gui] @ git+...` is refused by pip."""
    files = [ROOT / "README.md", *(ROOT / "src").rglob("*.py"), *(ROOT / "docs").rglob("*.md")]
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"pip install[^\n`]*", text):
            line = m.group(0)
            assert not re.search(r"['\"\s]dqp(\[|['\"\s]|$)", line), f"{f.name}: {line}"
    assert f'pip install "{DIST_NAME}[gui] @ git+' in (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_gui_files_are_package_data():
    gui = resources.files("dqp") / "gui"
    for name in ("app.py", "style.css", "examples/shop_orders.csv"):
        assert (gui / name).is_file(), name
    assert (resources.files("dqp") / "report" / "style.css").is_file()
    for pattern in ("report/*.css", "gui/*.css", "gui/examples/*.csv"):
        assert f'"{pattern}"' in PYPROJECT

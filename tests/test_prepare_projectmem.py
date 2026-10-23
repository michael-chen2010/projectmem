from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "prepare_projectmem.py"


@pytest.fixture
def preflight():
    assert SCRIPT_PATH.is_file(), (
        "prepare_projectmem.py must exist in the repository root"
    )
    spec = importlib.util.spec_from_file_location("prepare_projectmem", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_probe(tmp_path: Path) -> tuple[Path, dict]:
    source_root = (tmp_path / "checkout").resolve()
    package = source_root / "src" / "projectmem" / "__init__.py"
    server = source_root / "src" / "projectmem" / "mcp_server.py"
    first_project = tmp_path / "WebGameServer"
    second_project = tmp_path / "TableTennisGo"
    first_project.mkdir()
    second_project.mkdir()
    probe = {
        "python": sys.executable,
        "package_file": str(package),
        "mcp_server_file": str(server),
        "global_mode": True,
        "project_root": None,
        "projects": [
            {
                "id": "webgameserver",
                "path": str(first_project),
                "exists": True,
            },
            {
                "id": "tabletennisgo",
                "path": str(second_project),
                "exists": True,
            },
        ],
    }
    return source_root, probe


def test_child_environment_prioritizes_source_and_clears_single_project_pin(
    preflight, tmp_path
):
    source_root = tmp_path / "projectmem"
    other_src = tmp_path / "other-src"
    tools_dir = tmp_path / "tools"
    existing_path = os.pathsep.join((str(other_src), str(tools_dir)))
    original = {
        "PATH": str(tmp_path / "bin"),
        "PYTHONPATH": existing_path,
        "PROJECTMEM_ROOT": str(tmp_path / "WebGameServer"),
        "PROJECTMEM_HOME": str(tmp_path / ".projectmem"),
    }

    env = preflight.build_child_environment(source_root, original)

    parts = env["PYTHONPATH"].split(os.pathsep)
    assert Path(parts[0]) == source_root.resolve() / "src"
    assert parts[1:] == existing_path.split(os.pathsep)
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "PROJECTMEM_ROOT" not in env
    assert env["PROJECTMEM_HOME"] == original["PROJECTMEM_HOME"]
    assert original["PROJECTMEM_ROOT"] == str(tmp_path / "WebGameServer")


def test_validate_probe_accepts_source_import_and_global_multi_project_mode(
    preflight, tmp_path
):
    source_root, probe = make_probe(tmp_path)

    preflight.validate_probe(probe, source_root)

    assert len(probe["projects"]) == 2


@pytest.mark.parametrize(
    ("override_key", "override_value", "message"),
    [
        ("mcp_server_file", "installed-module", "src directory"),
        ("global_mode", False, "global"),
        (
            "projects",
            [
                {
                    "id": "webgameserver",
                    "path": "unused",
                    "exists": True,
                }
            ],
            "two",
        ),
    ],
)
def test_validate_probe_rejects_wrong_install_or_pinned_mode(
    preflight, tmp_path, override_key, override_value, message
):
    source_root, probe = make_probe(tmp_path)
    if override_key == "mcp_server_file":
        probe[override_key] = str(
            tmp_path / "site-packages" / "projectmem" / "mcp_server.py"
        )
    else:
        probe[override_key] = override_value

    with pytest.raises(preflight.PreflightError, match=message):
        preflight.validate_probe(probe, source_root)

#!/usr/bin/env python3
"""Validate projectmem source routing before starting tunnel-client.

Run from any directory with the Python interpreter that tunnel-client will use:
    python prepare_projectmem.py
    python prepare_projectmem.py --json

The JSON mode emits only the environment values PowerShell should apply to its
own process, plus evidence showing which source modules and projects were found.
A child Python process cannot mutate its parent PowerShell environment itself.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping


class PreflightError(RuntimeError):
    """Raised when the MCP server would not use the expected source/configuration."""


_PROBE_CODE = r"""
import json
import sys
from pathlib import Path

import projectmem
import projectmem.mcp_server as server
from projectmem.project_registry import load_registry

registry = load_registry()
print(json.dumps({
    "python": str(Path(sys.executable).resolve()),
    "package_file": str(Path(projectmem.__file__).resolve()),
    "mcp_server_file": str(Path(server.__file__).resolve()),
    "global_mode": bool(server.GLOBAL_MODE),
    "project_root": (
        str(Path(server._PROJECT_ROOT).resolve())
        if server._PROJECT_ROOT is not None else None
    ),
    "projects": [
        {
            "id": record.id,
            "name": record.name,
            "path": str(record.path.resolve()),
            "exists": record.path.is_dir(),
        }
        for record in registry.projects
    ],
}, ensure_ascii=True))
"""


def build_child_environment(
    repository_root: Path, base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build the environment to use for MCP startup and source verification."""
    env = dict(os.environ if base_env is None else base_env)
    source_path = str((Path(repository_root).resolve() / "src").resolve())
    source_key = os.path.normcase(os.path.normpath(source_path))

    search_paths = [source_path]
    for item in env.get("PYTHONPATH", "").split(os.pathsep):
        if not item:
            continue
        item_key = os.path.normcase(os.path.normpath(str(Path(item).resolve())))
        if item_key != source_key:
            search_paths.append(item)

    env["PYTHONPATH"] = os.pathsep.join(search_paths)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # A global registry must choose projects per MCP call, not pin this server.
    env.pop("PROJECTMEM_ROOT", None)
    return env


def _is_within(path: str, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def validate_probe(probe: dict, repository_root: Path) -> None:
    """Reject an installed-package import, single-project pin, or bad registry."""
    expected_source = (Path(repository_root).resolve() / "src").resolve()
    for key, label in (
        ("package_file", "projectmem package"),
        ("mcp_server_file", "MCP Server module"),
    ):
        path = probe.get(key)
        if not isinstance(path, str) or not _is_within(path, expected_source):
            raise PreflightError(
                f"{label} did not load from this repository's src directory: "
                f"{path!r}. Expected a path beneath {expected_source}."
            )

    if probe.get("global_mode") is not True or probe.get("project_root") is not None:
        raise PreflightError(
            "MCP Server is not in global multi-project mode. Remove --root and "
            "PROJECTMEM_ROOT from its startup configuration."
        )

    projects = probe.get("projects")
    if not isinstance(projects, list) or len(projects) < 2:
        count = len(projects) if isinstance(projects, list) else 0
        raise PreflightError(
            f"Expected at least two registered projects for shared MCP mode; "
            f"found {count}. Check the registry used by this Python process."
        )

    for project in projects:
        if not project.get("exists"):
            raise PreflightError(
                f"Registered project path does not exist: {project.get('path')!r}"
            )


def run_preflight(
    repository_root: Path, base_env: Mapping[str, str] | None = None
) -> dict:
    """Verify imports and routing using the exact interpreter running this script."""
    repository_root = Path(repository_root).resolve()
    source_root = repository_root / "src" / "projectmem"
    required_files = (
        source_root / "__init__.py",
        source_root / "mcp_server.py",
        source_root / "project_registry.py",
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise PreflightError(
            "Required projectmem source files are missing:\n  " + "\n  ".join(missing)
        )

    env = build_child_environment(repository_root, base_env)
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE_CODE],
            cwd=str(repository_root),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"Could not probe the MCP Server import: {exc}") from exc

    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        if len(details) > 4000:
            details = details[-4000:]
        raise PreflightError(
            "The MCP Server source could not be imported successfully. "
            "Check Python dependencies and the registry.\n" + details
        )

    try:
        probe = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise PreflightError(
            "The MCP source probe returned invalid JSON. Output was: "
            + result.stdout[-2000:]
        ) from exc

    validate_probe(probe, repository_root)
    return {
        "environment": {
            "PYTHONPATH": env["PYTHONPATH"],
            "PYTHONUTF8": env["PYTHONUTF8"],
            "PYTHONIOENCODING": env["PYTHONIOENCODING"],
        },
        "unset_environment": ["PROJECTMEM_ROOT"],
        "repository_root": str(repository_root),
        "source_dir": str(repository_root / "src"),
        **probe,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify projectmem source and multi-project MCP configuration."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable environment and verification evidence",
    )
    args = parser.parse_args(argv)

    try:
        report = run_preflight(Path(__file__).resolve().parent)
    except PreflightError as exc:
        print(f"projectmem preflight FAILED: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=True))
        return 0

    print("projectmem preflight PASSED")
    print(f"Python:       {report['python']}")
    print(f"Repository:    {report['repository_root']}")
    print(f"Source dir:    {report['source_dir']}")
    print(f"Package:      {report['package_file']}")
    print(f"MCP module:   {report['mcp_server_file']}")
    print("Routing:      global multi-project mode")
    print("Registered projects:")
    for project in report["projects"]:
        print(f"  - {project['id']}: {project['path']}")
    print()
    print("启动方式：在 PowerShell 中运行 .\\start_tunnel_client.ps1。")
    print("启动脚本会应用已验证的环境变量，并执行 tunnel-client init 和 run。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Smoke-test the build VM's read-only toolset + writable overlay + snapshot bind mount flow.

Run with the project's Python environment:
    PYTHONPATH=src .build-macos/venv/bin/python scripts/test_build_overlay_mount.py

The script reads the active Library's Build machine, toolset and Snapshots directory. It starts the machine when
needed, creates a temporary workspace, verifies Catalyst's stage1 compatibility patch in the writable overlay (or
optionally builds the snapshot), then unmounts and removes the workspace.
"""
from __future__ import annotations

import os
import shlex
import sys
import types
import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    # Meson installs these modules flat into catalystlab/, so mirror that import layout while running from source.
    package.__path__ = [str(SOURCE)] + [str(path) for path in SOURCE.rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.repository import Repository, library_path  # noqa: E402
from catalystlab.settings import Settings  # noqa: E402
from catalystlab.build_machine import BuildMachine  # noqa: E402
from catalystlab.rootless import (  # noqa: E402
    MachineExecutor,
    prepare_toolset_squashfs,
    unmount_read_only_mount,
    remove_in_namespace,
)
from catalystlab.project_build_rootless import (  # noqa: E402
    catalyst_snapshot_command,
    stage_build_session_script,
)

# Avoid importing the full repository registry, which loads GTK views. The executor and machine only need settings.
Repository.Settings = SimpleNamespace(value=Settings())

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="run Catalyst's stable snapshot target instead of the no-op mount check",
    )
    args = parser.parse_args()
    machines = BuildMachine.load_all()
    if not machines:
        print("No build machine is configured in the active Library", file=sys.stderr)
        return 2
    toolset_root = library_path("Toolsets")
    toolsets = []
    if os.path.isdir(toolset_root):
        for entry in os.scandir(toolset_root):
            try:
                with open(os.path.join(entry.path, "metadata.json"), encoding="utf-8") as file:
                    metadata = json.load(file)
                if metadata.get("machine_id"):
                    metadata["file_path"] = os.path.join(entry.path, "toolset.sqfs")
                    toolsets.append(metadata)
            except (OSError, ValueError):
                continue
    if not toolsets:
        print("No VM-linked toolset is configured in the active Library", file=sys.stderr)
        return 2

    machine = next((machine for machine in machines
                    if any(toolset["machine_id"] == str(machine.id) for toolset in toolsets)), None)
    if machine is None:
        print("No VM-linked toolset belongs to a configured machine", file=sys.stderr)
        return 2
    was_running = machine.is_running
    toolset = next((item for item in toolsets if item["machine_id"] == str(machine.id)), None)
    if toolset is None:
        print(f"No VM-linked toolset belongs to {machine.name}", file=sys.stderr)
        return 2
    toolset_path = toolset["file_path"]
    if not os.path.isfile(toolset_path):
        print(f"Toolset image is missing: {toolset_path}", file=sys.stderr)
        return 2

    snapshots = os.path.realpath(os.path.expanduser(library_path("Snapshots")))
    os.makedirs(snapshots, exist_ok=True)
    executor = MachineExecutor(machine)
    output = lambda line: print(line, flush=True)
    mounted_toolset = False
    temporary_toolset = False
    success = False
    workspace_acquired = False
    snapshot_test_started = time.time()
    try:
        machine.ensure_running(output)
        machine.acquire_workspace(output, user="Overlay smoke test")
        workspace_acquired = True
        toolset_root, mounted_toolset, temporary_toolset = prepare_toolset_squashfs(
            toolset_path, output, executor=executor)
        command = (
            "sh -c " + shlex.quote(
                "cat /etc/resolv.conf; python3 -c "
                + shlex.quote("import socket; print(socket.gethostbyname('anongit.gentoo.org'))")
                + " || true; " + catalyst_snapshot_command()
            )
            if args.snapshot else (
                'test -d /var/tmp/catalyst/snapshots && '
                'test -d /var/tmp && '
                'grep -Fq \'sys.stdout.write(str(b) + " ")\' '
                '/usr/share/catalyst/targets/stage1/build.py && '
                'echo "Overlay mount and stage1 compatibility patch smoke test passed"'
            )
        )
        script = stage_build_session_script(
            executor=executor,
            toolset_path=toolset_root,
            bindings=[(snapshots, "/var/tmp/catalyst/snapshots")],
            command=command,
            overlay_file_replacements=[(
                "/usr/share/catalyst/targets/stage1/build.py",
                'sys.stdout.write(b + " ")',
                'sys.stdout.write(str(b) + " ")',
            )],
        )
        success = executor.run_in_namespace(script, output)
        if args.snapshot and success:
            for name in os.listdir(snapshots):
                path = os.path.join(snapshots, name)
                if name.endswith(".lock") and os.path.getmtime(path) >= snapshot_test_started:
                    os.remove(path)
        return 0 if success else 1
    finally:
        if mounted_toolset:
            unmount_read_only_mount(toolset_root, output, executor=executor)
        elif temporary_toolset:
            remove_in_namespace([toolset_root], output, executor=executor)
        if workspace_acquired:
            machine.release_workspace(output, user="Overlay smoke test")
        if not was_running and machine.is_running:
            machine.stop(output)


if __name__ == "__main__":
    raise SystemExit(main())

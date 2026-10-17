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
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if "catalystlab" not in sys.modules:
    package = types.ModuleType("catalystlab")
    # Meson installs these modules flat into catalystlab/, so mirror that import layout while running from source.
    package.__path__ = [str(SOURCE)] + [str(path) for path in SOURCE.rglob("*") if path.is_dir()]
    sys.modules["catalystlab"] = package

from catalystlab.repository import Repository, library_path  # noqa: E402
from catalystlab.settings import Settings  # noqa: E402
from catalystlab.rootless import (  # noqa: E402
    MachineExecutor,
    prepare_toolset_squashfs,
    unmount_squashfs,
)
from catalystlab.lima import (  # noqa: E402
    MACHINE_DATA_DIRECTORY,
    lima_environment,
    limactl_path,
    move_lima_disk_to_temporary,
    remove_temporary_lima_disk_data,
    run_limactl,
    workspace_image_path,
)
from catalystlab.project_build_rootless import (  # noqa: E402
    catalyst_snapshot_command,
    stage_build_session_script,
)

# Importing the full repositories module loads Gtk through Toolset, which aborts on macOS when this
# diagnostic script is run without an application GUI. The executor only needs the configured paths.
Repository.Settings = SimpleNamespace(value=Settings())


class Machine:
    """Small adapter for MachineExecutor, populated from the active Library's machine metadata."""

    STATUS_MISSING = "Missing"

    def __init__(self, metadata: dict):
        self.id = metadata["id"]
        self.name = metadata["name"]
        self.lima_home = metadata["lima_home"]
        self.instance_name = f"catalystlab-{self.id.replace('-', '')[:8]}"
        self.workspace_gib = metadata.get("workspace_gib", 64)
        self.swap_gib = metadata.get("swap_gib", 16)
        self._running = False
        self._was_running = None

    def begin_use(self, _user="Command"):
        pass

    def end_use(self, _user="Command"):
        pass

    def ensure_running(self, output_handler=print, process_holder=None):
        path = limactl_path()
        if path is None:
            raise RuntimeError("Lima is not installed")
        result = subprocess.run(
            [path, "list", "--json"], capture_output=True, text=True,
            env=lima_environment(self.lima_home), check=True,
        )
        running = any(
            (instance := json.loads(line)).get("name") == self.instance_name
            and instance.get("status", "").lower() == "running"
            for line in result.stdout.splitlines() if line.strip()
        )
        if self._was_running is None:
            self._was_running = running
        self._running = running
        if not running and self.swap_gib:
            disk_name = f"{self.instance_name}-swap"
            run_limactl(["disk", "delete", "--force", disk_name], lambda _line: None,
                        home=self.lima_home)
            remove_temporary_lima_disk_data(disk_name, self.lima_home)
            if not run_limactl(["disk", "create", disk_name, f"--size={self.swap_gib}GiB", "--format=raw"],
                               output_handler, process_holder, home=self.lima_home):
                raise RuntimeError(f"Failed to create swap disk for {self.name}")
            move_lima_disk_to_temporary(disk_name, self.lima_home)
            config_path = os.path.join(self.lima_home, self.instance_name, "lima.yaml")
            try:
                with open(config_path, encoding="utf-8") as file:
                    attached = disk_name in file.read()
            except OSError:
                attached = False
            if not attached:
                expression = f'.additionalDisks = [{{"name": "{disk_name}", "format": false}}]'
                if not run_limactl(["edit", "--set", expression, self.instance_name], output_handler,
                                   process_holder, home=self.lima_home):
                    raise RuntimeError(f"Failed to attach swap disk for {self.name}")
        if not running:
            output_handler(f"Starting virtual machine {self.name}")
            if not run_limactl(["start", self.instance_name], output_handler, process_holder,
                               home=self.lima_home):
                raise RuntimeError(f"Failed to start {self.name}")

    def acquire_workspace(self, output_handler=print, process_holder=None, user="Overlay smoke test"):
        image = workspace_image_path(self.instance_name)
        os.makedirs(os.path.dirname(image), exist_ok=True)
        output_handler(f"Preparing working space of {self.name}")
        script = f'''
set -e
sudo umount -R -l {MACHINE_DATA_DIRECTORY} 2>/dev/null || true
mkdir -p {shlex.quote(os.path.dirname(image))}
rm -f {json.dumps(image)}
truncate -s {self.workspace_gib}G {json.dumps(image)}
mkfs.ext4 -q -F -m 0 {json.dumps(image)}
sudo mount -o loop {json.dumps(image)} {MACHINE_DATA_DIRECTORY}
sudo chown "$(id -u):$(id -g)" {MACHINE_DATA_DIRECTORY}
'''
        if not MachineExecutor(self).run(script, output_handler, process_holder):
            raise RuntimeError(f"Failed to prepare working space of {self.name}")

    def release_workspace(self, output_handler=print, user="Overlay smoke test"):
        image = workspace_image_path(self.instance_name)
        if os.path.exists(image):
            output_handler(f"Removing working space of {self.name}")
            script = f'''
set -e
if mountpoint -q {MACHINE_DATA_DIRECTORY}; then sudo umount -R -l {MACHINE_DATA_DIRECTORY}; fi
rm -f {json.dumps(image)}
'''
            if not MachineExecutor(self).run(script, output_handler):
                output_handler(f"Failed to remove working space of {self.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="run Catalyst's stable snapshot target instead of the no-op mount check",
    )
    args = parser.parse_args()
    machine_root = library_path("Machines")
    machine_metadata = []
    if os.path.isdir(machine_root):
        for entry in os.scandir(machine_root):
            metadata_path = os.path.join(entry.path, "catalystlab.json")
            try:
                with open(metadata_path, encoding="utf-8") as file:
                    machine_metadata.append(json.load(file))
            except (OSError, ValueError):
                continue
    if not machine_metadata:
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

    machine_data = next((item for item in machine_metadata
                         if any(toolset["machine_id"] == item["id"] for toolset in toolsets)), None)
    machine = Machine(machine_data) if machine_data else None
    toolset = next((item for item in toolsets if machine and item["machine_id"] == machine.id), None)
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
    success = False
    workspace_acquired = False
    snapshot_test_started = time.time()
    try:
        machine.ensure_running(output)
        machine.acquire_workspace(output, user="Overlay smoke test")
        workspace_acquired = True
        toolset_root, mounted_toolset = prepare_toolset_squashfs(toolset_path, output, executor=executor)
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
            unmount_squashfs(toolset_root, output, executor=executor)
        if workspace_acquired:
            machine.release_workspace(output, user="Overlay smoke test")
        if not machine._was_running:
            path = limactl_path()
            stopped = subprocess.run(
                [path, "stop", machine.instance_name, "--tty=false"],
                env=lima_environment(machine.lima_home), check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
            if stopped.returncode == 0 and machine.swap_gib:
                disk_name = f"{machine.instance_name}-swap"
                run_limactl(["disk", "delete", "--force", disk_name], output, home=machine.lima_home)
                remove_temporary_lima_disk_data(disk_name, machine.lima_home)


if __name__ == "__main__":
    raise SystemExit(main())

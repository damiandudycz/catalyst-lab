"""Read-only storage summaries and guarded removal helpers for the selected Library."""
from __future__ import annotations

import os
import json
import shutil
from datetime import datetime
import re


LIBRARY_DESCRIPTIONS = {
    "Toolsets": "Reusable Gentoo environments used to build and run stages.",
    "Snapshots": "Cached Gentoo repository snapshots used for reproducible builds.",
    "Releng": "Gentoo Release Engineering templates and configuration.",
    "Overlays": "Portage overlay repositories and their local changes.",
    "Projects": "Project definitions, stage configuration, and project-local files.",
    "Builds": "Built stage artifacts, build records, and logs.",
    "Packages": "Binary package caches shared between compatible builds.",
    "Runners": "VM runner definitions and persistent runner disks.",
    "Cache": "Reusable downloads, extracted systems, VM images, and kernel outputs.",
    "Temporary": "Working files for operations and VM runs; normally cleared on app startup.",
}

CACHE_DESCRIPTIONS = {
    "catalyst-seeds": "Downloaded Gentoo stage archives used as seed systems when building new stages.",
    "Extracted": "Reusable extracted copies of toolsets and Gentoo snapshots, avoiding repeated extraction.",
    "Packages": "Binary package cache synchronized from runners for reuse by later builds.",
    "Projects": "Per-project build caches synchronized from runners, such as stage package caches.",
    "ToolsetStages": "Downloaded Gentoo stage archives used as the starting point when creating toolsets.",
    "ToolsetBuilds": "Intermediate SquashFS files produced while a new toolset is being compressed.",
    "TestVMImages": "Pristine VM base images prepared from completed stages and reused by test runs.",
    "TestVMKernels": "Reusable custom kernel, initramfs, and module outputs for VM runs.",
    "Distfiles": "Source archives downloaded by Portage and reused by later package builds.",
}
CACHE_TITLES = {
    "catalyst-seeds": "Gentoo seed stages",
    "Extracted": "Extracted toolsets and snapshots",
    "Packages": "Shared binary package cache",
    "Projects": "Per-project build caches",
    "ToolsetStages": "Toolset source stages",
    "ToolsetBuilds": "Toolset compression work",
    "TestVMImages": "Prepared VM base images",
    "TestVMKernels": "Reusable VM kernels",
    "Distfiles": "Portage source archives",
}

TEMPORARY_DESCRIPTIONS = {
    "Runners": "VM runner workspace disks and state used while compiling stages or toolsets.",
    "MachineCommands": "Temporary command scripts transferred to runners for execution.",
    "Sessions": "Active build and toolset session data, including writable filesystem overlays.",
    "TestVMImages": "Writable per-run VM disks and configuration files removed after the run ends.",
    "VMLogs": "Diagnostic logs collected from VM startup, guest behavior, and shutdown.",
    "Work": "Extracted writable toolset copies and intermediate operation files.",
}
TEMPORARY_TITLES = {
    "Runners": "VM runner workspaces",
    "MachineCommands": "Runner command files",
    "Sessions": "Operation sessions",
    "TestVMImages": "Writable VM run images",
    "VMLogs": "Virtual machine logs",
    "Work": "Operation working files",
}
OBJECT_USAGE = {
    "catalyst-seeds": "Used as the starting Gentoo stage when a build has no parent stage available.",
    "Distfiles": "Shared source downloads used by Portage package builds and custom-kernel builds; not tied to one project.",
    "Extracted": "Reused by operations that need the matching toolset or snapshot extracted from SquashFS.",
    "Packages": "Reused by compatible projects and stages that use this binary-package folder.",
    "Projects": "Reused by later builds of the owning project on its runner.",
    "ToolsetStages": "Used as the source stage when creating or recreating a toolset.",
    "ToolsetBuilds": "Intermediate output from toolset creation; normally only useful while compression is in progress.",
    "TestVMImages": "Pristine stage disk image reused when starting a VM test for the same build and VM settings.",
    "TestVMKernels": "Reusable kernel output for VM runs with the same architecture, package, and toolset.",
    "Runners": "Workspace data used by the corresponding VM runner.",
    "MachineCommands": "Command files consumed by runner operations.",
    "Sessions": "Temporary state used by build and toolset operations.",
    "VMLogs": "Diagnostic output from the corresponding VM run; retained for troubleshooting.",
    "Work": "Temporary extracted trees and intermediate files used by operations.",
}


def _allocated_size(info: os.stat_result) -> int:
    """Count allocated bytes when available so sparse VM disks aren't reported at their maximum size."""
    return min(info.st_size, info.st_blocks * 512) if hasattr(info, "st_blocks") else info.st_size


def _measure(path: str) -> tuple[int, dict[str, int]]:
    """Return allocated bytes and sizes of immediate children, without following symlinks."""
    try:
        root_info = os.lstat(path)
    except OSError:
        return 0, {}
    if not os.path.isdir(path) or os.path.islink(path):
        return _allocated_size(root_info), {}

    total = 0
    children: dict[str, int] = {}
    for directory, folders, files in os.walk(path, topdown=True, followlinks=False):
        relative = os.path.relpath(directory, path)
        for folder in list(folders):
            item = os.path.join(directory, folder)
            try:
                info = os.lstat(item)
            except OSError:
                continue
            if os.path.islink(item):
                folders.remove(folder)
                size = _allocated_size(info)
                total += size
                if relative == ".":
                    children[folder] = children.get(folder, 0) + size
        for filename in files:
            item = os.path.join(directory, filename)
            try:
                size = _allocated_size(os.lstat(item))
            except OSError:
                continue
            total += size
            if relative == ".":
                children[filename] = children.get(filename, 0) + size
            else:
                first = relative.split(os.sep, 1)[0]
                children[first] = children.get(first, 0) + size
    return total, children


def _human_modified(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.lstat(path).st_mtime).astimezone().strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "unknown"


def _cache_object_details(category: str, path: str, relative: str, is_dir: bool) -> str:
    details = ["Folder" if is_dir else "File", f"Modified {_human_modified(path)}"]
    if category == "TestVMKernels" and is_dir:
        metadata_path = os.path.join(path, "kernel-build.json")
        try:
            with open(metadata_path, encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            parts = [metadata.get("architecture"), metadata.get("kernel_package"), metadata.get("kernel_release")]
            summary = " · ".join(str(part) for part in parts if part)
            if summary:
                details.append(summary)
        except (OSError, ValueError, TypeError):
            pass
    elif category == "TestVMImages" and is_dir:
        details.append("Prepared stage VM base image")
    elif category == "Extracted":
        details.append("Reusable extracted toolset or snapshot")
    elif category in ("Packages", "Projects"):
        details.append(f"Cache path: {relative}")
    elif category in ("Distfiles", "catalyst-seeds", "ToolsetStages"):
        details.append(os.path.basename(path))
    return " · ".join(details)


def _cache_object_metadata(category: str, path: str, relative: str) -> list[str]:
    metadata = []
    if category == "TestVMKernels":
        try:
            with open(os.path.join(path, "kernel-build.json"), encoding="utf-8") as metadata_file:
                data = json.load(metadata_file)
            for label, value in (
                ("Architecture", data.get("architecture")),
                ("Kernel package", data.get("kernel_package")),
                ("Kernel release", data.get("kernel_release")),
                ("Built at", data.get("built_at")),
                ("Gentoo snapshot", data.get("gentoo_snapshot")),
                ("Stage", (data.get("stage") or {}).get("name")),
                ("Toolset", (data.get("toolset") or {}).get("name")),
            ):
                if value:
                    metadata.append(f"{label}: {value}")
            binary_cache = data.get("binary_package_cache") or {}
            if binary_cache.get("enabled") and binary_cache.get("path"):
                metadata.append(f"Binary package cache: {binary_cache['path']}")
            overlays = data.get("overlays") or []
            if overlays:
                metadata.append("Overlays: " + ", ".join(str(item) for item in overlays))
        except (OSError, ValueError, TypeError):
            pass
    elif category == "TestVMImages" and os.path.isdir(path):
        contents = []
        for name in ("root.img", "kernel", "initramfs"):
            item = os.path.join(path, name)
            if os.path.isfile(item):
                contents.append(f"{name} ({_human_size(_allocated_size(os.lstat(item)))})")
        metadata.append("Contains: " + (", ".join(contents) if contents else "no recognized image files"))
        metadata.append("Image key: " + os.path.basename(path))
    elif category == "Extracted" and relative.endswith(".ext4"):
        machine_prefix = relative.rsplit("/", 1)[-1].removesuffix(".ext4")
        if "-" in machine_prefix:
            metadata.append(f"VM runner cache image: {machine_prefix.rsplit('-', 1)[0]}")
    elif category in ("Packages", "Projects"):
        metadata.append("Cache key: " + relative)
    return metadata


def _human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def _project_references(library_path: str, key: str, value) -> list[str]:
    """Find references saved in known project Git metadata files."""
    result = []
    try:
        projects = list(os.scandir(os.path.join(library_path, "Projects")))
    except OSError:
        return result
    for project in projects:
        if project.name.startswith(".") or not project.is_dir(follow_symlinks=False):
            continue
        try:
            git_path = os.path.join(project.path, ".git")
            if os.path.isfile(git_path):
                with open(git_path, encoding="utf-8") as file:
                    git_entry = file.readline().strip()
                if not git_entry.startswith("gitdir:"):
                    continue
                git_path = os.path.normpath(os.path.join(project.path, git_entry.split(":", 1)[1].strip()))
            with open(os.path.join(git_path, "catalystlab.json"), encoding="utf-8") as file:
                data = json.load(file)
            metadata = data.get("metadata") or {}
            # A project configured to use the latest snapshot follows the
            # newest repository snapshot dynamically; it does not pin this file.
            if key == "snapshot_id" and metadata.get("latest_snapshot"):
                continue
            if metadata.get(key) == value:
                result.append(data.get("name") or project.name)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return result


def _registered_binpkgs(library_path: str) -> list[dict]:
    """List registered shared binary-package directories, using their saved metadata."""
    root = os.path.join(library_path, "Packages")
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    items = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        metadata_path = os.path.join(entry.path, "metadata.json")
        try:
            with open(metadata_path, encoding="utf-8") as file:
                metadata = json.load(file)
            if not isinstance(metadata, dict) or not metadata.get("id") or not metadata.get("name"):
                continue
            if os.path.realpath(entry.path) != os.path.realpath(os.path.join(root, str(metadata["name"]).replace("/", "_"))):
                continue
        except (OSError, ValueError, TypeError):
            continue
        size = _measure(entry.path)[0]
        if not size:
            continue
        projects = _project_references(library_path, "packages_directory_id", str(metadata["id"]))
        architecture = metadata.get("architecture") or "Any architecture"
        flags = metadata.get("cpu_flags") or "Default CPU flags"
        created = metadata.get("created")
        details = [architecture, flags, f"Modified {_human_modified(entry.path)}"]
        if created:
            details.append(f"Created {created}")
        items.append({
            "name": metadata["name"], "relative_path": entry.name, "category": "registered",
            "path": entry.path, "size": size, "is_dir": True, "type_title": "Binary package folders",
            "description": "Shared Portage binary packages organized by release type and reused by compatible projects.",
            "usage": "Used by: " + (", ".join(projects) if projects else "no project currently references this folder"),
            "projects": projects,
            "in_use": bool(projects),
            "in_use_reason": "Referenced by project(s): " + ", ".join(projects) if projects else None,
            "metadata": [f"ID: {metadata['id']}", f"Architecture: {architecture}", f"CPU flags: {flags}",
                         *( [f"Created: {created}"] if created else [] ),
                         *( ["Projects: " + ", ".join(projects)] if projects else [] )],
            "details": f"{architecture} · {flags} · " + (f"Used by {len(projects)} project(s)" if projects else "Not in use"),
        })
    return sorted(items, key=lambda item: item["name"].casefold())


def _snapshots(library_path: str) -> list[dict]:
    """List direct SquashFS snapshot files recognized by Snapshot.load_all."""
    root = os.path.join(library_path, "Snapshots")
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    items = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    snapshot_entries = [entry for entry in entries
                        if not entry.name.startswith(".") and entry.name.endswith(".sqfs")
                        and entry.is_file(follow_symlinks=False)]
    for entry in snapshot_entries:
        try:
            info = entry.stat(follow_symlinks=False)
            marker_path = entry.path + ".json"
            from_file = False
            try:
                with open(marker_path, encoding="utf-8") as file:
                    from_file = bool(json.load(file).get("from_file", False))
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError):
                pass
            generated = re.fullmatch(r"gentoo-([0-9a-f]{40})\.sqfs", entry.name)
            details = "Imported snapshot" if from_file else ("Gentoo repository snapshot" if generated else "Snapshot file")
            projects = _project_references(library_path, "snapshot_id", entry.name)
            projects = sorted(set(projects), key=str.casefold)
            metadata = [f"Filename: {entry.name}", f"Modified {_human_modified(entry.path)}"]
            if generated:
                metadata.append(f"Gentoo commit: {generated.group(1)}")
            if from_file:
                metadata.append("Imported from an existing snapshot file")
            if projects:
                metadata.append("Referenced by project(s): " + ", ".join(projects))
            items.append({
                "name": entry.name, "relative_path": entry.name, "category": "snapshot",
                "path": entry.path, "size": _allocated_size(info), "is_dir": False,
                "type_title": "Gentoo snapshots",
                "description": "SquashFS snapshots of the Gentoo repository used by stage and kernel builds.",
                "usage": ("Referenced by project(s): " + ", ".join(projects)) if projects else
                         "Available to projects, stages, and kernel builds that need a reproducible Gentoo repository state.",
                "projects": projects,
                "in_use": bool(projects),
                "in_use_reason": "Referenced by project(s): " + ", ".join(projects) if projects else None,
                "metadata": metadata, "details": f"{details} · Modified {_human_modified(entry.path)}",
            })
        except OSError:
            continue
    return sorted(items, key=lambda item: item["relative_path"].casefold())


def _projects(library_path: str) -> list[dict]:
    """List project directories registered by Catalyst Lab Git metadata."""
    root = os.path.join(library_path, "Projects")
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    items = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        metadata_path = os.path.join(entry.path, ".git", "catalystlab.json")
        git_path = os.path.join(entry.path, ".git")
        try:
            if os.path.isfile(git_path):
                with open(git_path, encoding="utf-8") as file:
                    git_entry = file.readline().strip()
                if not git_entry.startswith("gitdir:"):
                    continue
                metadata_path = os.path.normpath(os.path.join(
                    entry.path, git_entry.split(":", 1)[1].strip(), "catalystlab.json"))
            with open(metadata_path, encoding="utf-8") as file:
                data = json.load(file)
            metadata = data.get("metadata") or {}
            if not data.get("id") or not data.get("name") or not isinstance(metadata, dict):
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue

        stage_names = []
        stages_root = os.path.join(entry.path, "stages")
        try:
            for stage in os.scandir(stages_root):
                if stage.name.startswith(".") or not stage.is_dir(follow_symlinks=False):
                    continue
                stage_names.append(stage.name)
        except OSError:
            pass
        project_builds = os.path.join(library_path, "Builds", data["id"].replace("-", ""), "stages")
        build_count = 0
        try:
            for stage in os.scandir(project_builds):
                if not stage.is_dir(follow_symlinks=False):
                    continue
                for build in os.scandir(stage.path):
                    if build.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(build.path, "build.json")):
                        build_count += 1
        except OSError:
            pass
        package_id = metadata.get("packages_directory_id")
        package_name = None
        if package_id:
            for package in _registered_binpkgs(library_path):
                try:
                    with open(os.path.join(package["path"], "metadata.json"), encoding="utf-8") as file:
                        package_metadata = json.load(file)
                    if str(package_metadata.get("id")) == str(package_id):
                        package_name = package["name"]
                        break
                except (OSError, ValueError, TypeError):
                    pass
        architecture = metadata.get("architecture") or "Not set"
        snapshot = "Latest snapshot" if metadata.get("latest_snapshot") else metadata.get("snapshot_id") or "Not selected"
        item_metadata = [f"Project ID: {data['id']}", f"Architecture: {architecture}",
                         f"Stages: {len(stage_names)}" + (" · " + ", ".join(sorted(stage_names, key=str.casefold)) if stage_names else ""),
                         f"Build records: {build_count}", f"Snapshot: {snapshot}",
                         f"Binary packages: {package_name or ('Not assigned' if not package_id else str(package_id))}",
                         f"Modified {_human_modified(entry.path)}"]
        items.append({
            "name": data["name"], "relative_path": entry.name, "category": "project",
            "path": entry.path, "size": _measure(entry.path)[0], "is_dir": True,
            "type_title": "Projects", "description": "Project Git files, stage definitions, and build configuration.",
            "usage": f"{len(stage_names)} stage(s) · {build_count} saved build record(s)",
            "metadata": item_metadata, "details": f"{architecture} · {len(stage_names)} stage(s) · {build_count} build(s)",
            "project_id": data["id"], "build_count": build_count,
        })
    return sorted(items, key=lambda item: item["name"].casefold())


def _builds(library_path: str) -> list[dict]:
    """List build records and their outputs individually, using only recognized build metadata."""
    root = os.path.join(library_path, "Builds")
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    items = []
    try:
        project_names = {item["project_id"].replace("-", ""): item["name"]
                         for item in _projects(library_path)}
    except (KeyError, TypeError):
        project_names = {}
    try:
        projects = list(os.scandir(root))
    except OSError:
        return []
    for project in projects:
        if project.name.startswith(".") or not project.is_dir(follow_symlinks=False):
            continue
        stages_root = os.path.join(project.path, "stages")
        try:
            stages = list(os.scandir(stages_root))
        except OSError:
            continue
        for stage in stages:
            if stage.name.startswith(".") or not stage.is_dir(follow_symlinks=False):
                continue
            try:
                builds = list(os.scandir(stage.path))
            except OSError:
                continue
            for build in builds:
                if build.name.startswith(".") or not build.is_dir(follow_symlinks=False):
                    continue
                metadata_path = os.path.join(build.path, "build.json")
                try:
                    with open(metadata_path, encoding="utf-8") as file:
                        metadata = json.load(file)
                    if not isinstance(metadata, dict) or not metadata.get("id"):
                        continue
                except (OSError, ValueError, TypeError):
                    continue
                details = [f"Status: {metadata.get('status', 'unknown')}",
                           f"Stage: {metadata.get('stage_name') or stage.name}",
                           f"Timestamp: {metadata.get('timestamp') or build.name}",
                           f"Modified {_human_modified(build.path)}"]
                artifact = metadata.get("artifact")
                if artifact:
                    artifact_path = os.path.join(build.path, artifact)
                    if os.path.isfile(artifact_path):
                        details.append(f"Artifact: {artifact} · {_human_size(_allocated_size(os.lstat(artifact_path)))}")
                relative = os.path.relpath(build.path, root)
                project_name = project_names.get(project.name, project.name)
                items.append({
                    "name": metadata.get("stage_name") or stage.name,
                    "relative_path": relative,
                    "category": "build",
                    "path": build.path,
                    "size": _measure(build.path)[0],
                    "is_dir": True,
                    "type_title": "Stage builds",
                    "description": "Built stage artifacts, logs, failure details, and reproducibility metadata.",
                    "usage": f"Project {project_name} · stage {metadata.get('stage_name') or stage.name}",
                    "metadata": [f"Project: {project_name}", *details],
                    "details": f"{metadata.get('status', 'unknown')} · {metadata.get('timestamp') or build.name}",
                    "project_key": project.name,
                    "project_name": project_name,
                    "timestamp": metadata.get("timestamp") or build.name,
                })
    return sorted(items, key=lambda item: (item["project_key"], item["relative_path"]), reverse=True)


def _walk_known_objects(root: str, category: str, section: str) -> list[dict]:
    """Return individually removable objects inside one recognized library category."""
    if not os.path.isdir(root) or os.path.islink(root):
        return []
    candidates: list[str] = []

    def children(directory: str) -> list[str]:
        try:
            return [entry.path for entry in os.scandir(directory)
                    if not entry.name.startswith(".") and not entry.is_symlink()]
        except OSError:
            return []

    direct_files = {"catalyst-seeds", "Distfiles", "ToolsetStages", "ToolsetBuilds", "MachineCommands", "VMLogs"}
    if category in direct_files:
        candidates = [path for path in children(root) if not os.path.isdir(path)]
        # Toolset build intermediates may be directories depending on the interrupted phase.
        if category == "ToolsetBuilds":
            candidates = children(root)
    elif category in {"TestVMImages", "TestVMKernels", "Runners", "Sessions", "Work"}:
        candidates = [path for path in children(root) if os.path.isdir(path)]
        if category == "TestVMImages":
            candidates = [path for path in candidates if os.path.isfile(os.path.join(path, ".ready"))]
        if category == "TestVMKernels":
            candidates = [path for path in candidates if os.path.isfile(os.path.join(path, "custom-kernel"))]
    elif category == "Extracted":
        for first in children(root):
            if os.path.basename(first) == "Toolsets" and os.path.isdir(first):
                candidates.extend(path for path in children(first) if path.endswith(".ext4"))
                candidates.extend(path for path in children(first) if path.endswith(".d") and os.path.isdir(path))
            elif os.path.basename(first) in {"Toolsets", "Snapshots"} and os.path.isdir(first):
                for path in children(first):
                    if path.endswith(".d") and os.path.isdir(path):
                        candidates.append(path)
        # Extraction caches can be directly under the kind folder too.
        candidates.extend(path for path in children(root) if path.endswith(".d") and os.path.isdir(path))
    elif category == "Packages":
        for owner in children(root):
            if os.path.isdir(owner):
                candidates.extend(path for path in children(owner) if os.path.isdir(path))
    elif category == "Projects":
        for project in children(root):
            caches = os.path.join(project, "caches")
            for cache in children(caches):
                if os.path.isdir(cache):
                    cache_entries = children(cache)
                    # Build-machine caches are grouped by cache kind and then by
                    # release/CPU flags; expose that leaf so cleanup stays narrow.
                    candidates.extend(path for path in cache_entries if os.path.isdir(path))
                    if not any(os.path.isdir(path) for path in cache_entries):
                        candidates.append(cache)
    else:
        candidates = children(root)

    results = []
    for path in sorted(set(candidates), key=str.casefold):
        relative = os.path.relpath(path, root)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        is_dir = os.path.isdir(path) and not os.path.islink(path)
        size = _measure(path)[0] if is_dir else _allocated_size(info)
        if size == 0:
            continue
        basename = os.path.basename(path)
        parts = relative.split(os.sep)
        if category == "Packages" and len(parts) >= 2:
            extra_detail = f"Package cache ID {parts[0]} · release type {parts[-1]}"
        elif category == "Projects" and len(parts) >= 4:
            extra_detail = f"Project ID {parts[0]} · {parts[-2]} · release type {parts[-1]}"
        else:
            extra_detail = None
        details = _cache_object_details(category, path, relative, is_dir) if section == "Cache" \
            else f"{'Folder' if is_dir else 'File'} · Modified {_human_modified(path)} · {relative}"
        if extra_detail:
            details = f"{details} · {extra_detail}"
        results.append({
            "name": basename,
            "relative_path": relative,
            "category": category,
            "path": path,
            "size": size,
            "is_dir": is_dir,
            "type_title": (CACHE_TITLES if section == "Cache" else TEMPORARY_TITLES)[category],
            "description": (CACHE_DESCRIPTIONS if section == "Cache" else TEMPORARY_DESCRIPTIONS)[category],
            "usage": OBJECT_USAGE[category],
            "metadata": _cache_object_metadata(category, path, relative) if section == "Cache" else [],
            "details": details,
        })
    return results


def scan_library_storage(library_path: str) -> dict:
    """Summarize known library sections and enumerate known cache/temp objects individually."""
    try:
        names = sorted(os.listdir(library_path), key=str.casefold)
    except OSError as error:
        return {"error": str(error), "total_size": 0, "sections": [], "cache_items": [], "temporary_items": []}

    sections = []
    measured: dict[str, tuple[int, dict[str, int]]] = {}
    # Show only library sections whose purpose the application knows. Keeping
    # this list explicit avoids exposing unrelated files in a user-selected
    # library directory as Catalyst Lab data.
    for name in names:
        if name not in LIBRARY_DESCRIPTIONS:
            continue
        path = os.path.join(library_path, name)
        size, children = _measure(path)
        measured[name] = (size, children)
        sections.append({"name": name, "path": path, "size": size,
                         "description": LIBRARY_DESCRIPTIONS[name]})

    def items_for(section: str, descriptions: dict[str, str]) -> list[dict]:
        root = os.path.join(library_path, section)
        if not os.path.isdir(root):
            return []
        result = []
        for category in descriptions:
            result.extend(_walk_known_objects(os.path.join(root, category), category, section))
        return result

    return {
        "total_size": sum(section["size"] for section in sections),
        "sections": sections,
        "cache_items": items_for("Cache", CACHE_DESCRIPTIONS),
        "temporary_items": items_for("Temporary", TEMPORARY_DESCRIPTIONS),
        "binpkg_items": _registered_binpkgs(library_path),
        "snapshot_items": _snapshots(library_path),
        "project_items": _projects(library_path),
        "build_items": _builds(library_path),
    }


def remove_library_item(library_path: str, section: str, category: str, relative_path: str) -> None:
    """Remove one enumerated object from a known Cache/Temporary category safely."""
    if section == "Builds":
        if category != "build" or not relative_path or os.path.isabs(relative_path):
            raise ValueError("Only recognized stage builds can be removed")
        parts = relative_path.split(os.sep)
        if len(parts) != 4 or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
            raise ValueError("The selected build path is invalid")
        root = os.path.realpath(os.path.join(library_path, "Builds"))
        target = os.path.join(root, *parts)
        real_target = os.path.realpath(target)
        components = [os.path.join(root, *parts[:index]) for index in range(1, len(parts) + 1)]
        if (os.path.islink(os.path.join(library_path, "Builds")) or any(os.path.islink(path) for path in components)
                or os.path.commonpath((root, real_target)) != root
                or not os.path.isfile(os.path.join(real_target, "build.json"))):
            raise ValueError("The selected item is not a recognized stage build")
        shutil.rmtree(real_target)
        stage_directory = os.path.dirname(real_target)
        if os.path.isdir(stage_directory) and not os.listdir(stage_directory):
            os.rmdir(stage_directory)
        return
    if section in {"Packages", "Snapshots"}:
        expected_category = "registered" if section == "Packages" else "snapshot"
        if category != expected_category or not relative_path or os.path.isabs(relative_path):
            raise ValueError("Only recognized package folders or snapshots can be removed")
        parts = relative_path.split(os.sep)
        if len(parts) != 1 or parts[0] in ("", ".", "..") or parts[0].startswith("."):
            raise ValueError("The selected item path is invalid")
        root = os.path.realpath(os.path.join(library_path, section))
        if os.path.islink(os.path.join(library_path, section)):
            raise ValueError("Library section symlinks cannot be cleaned up here")
        target = os.path.join(root, parts[0])
        if os.path.commonpath((root, os.path.realpath(os.path.dirname(target)))) != root:
            raise ValueError("The selected item is outside its library section")
        if section == "Packages":
            metadata_path = os.path.join(target, "metadata.json")
            try:
                with open(metadata_path, encoding="utf-8") as file:
                    metadata = json.load(file)
                if not metadata.get("id") or not metadata.get("name"):
                    raise ValueError("Package folder is not registered")
            except (OSError, ValueError, TypeError) as error:
                raise ValueError("Package folder is not registered") from error
            if os.path.realpath(target) != os.path.realpath(os.path.join(root, str(metadata["name"]).replace("/", "_"))):
                raise ValueError("Package folder metadata does not match its path")
        elif not parts[0].endswith(".sqfs"):
            raise ValueError("The selected file is not a known snapshot")
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return
        if section == "Snapshots" and (not os.path.isfile(target) or os.path.islink(target)):
            raise ValueError("The selected snapshot is not a regular file")
        if section == "Packages" and (not os.path.isdir(target) or os.path.islink(target)):
            raise ValueError("The selected package folder is not a directory")
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)
        if section == "Snapshots":
            marker = target + ".json"
            if os.path.isfile(marker) and not os.path.islink(marker):
                os.unlink(marker)
        return
    known_categories = {"Cache": CACHE_DESCRIPTIONS, "Temporary": TEMPORARY_DESCRIPTIONS}
    if section not in known_categories or category not in known_categories[section]:
        raise ValueError("Only known Cache or Temporary object types can be removed")
    if not relative_path or os.path.isabs(relative_path):
        raise ValueError("The selected item must be a relative path")
    parts = relative_path.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("The selected item path is invalid")
    section_root = os.path.realpath(os.path.join(library_path, section))
    category_path = os.path.join(section_root, category)
    if os.path.islink(category_path):
        raise ValueError("Library category symlinks cannot be cleaned up here")
    root = os.path.realpath(category_path)
    if os.path.commonpath((section_root, root)) != section_root:
        raise ValueError("The selected category is outside the library section")
    target = os.path.join(root, *parts)
    parent = os.path.realpath(os.path.dirname(target))
    if os.path.commonpath((root, parent)) != root:
        raise ValueError("The selected item is outside its library category")
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    else:
        os.unlink(target)

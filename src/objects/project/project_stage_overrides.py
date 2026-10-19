"""Per-stage default and explicit-override metadata stored in the Library, outside project Git trees."""
from __future__ import annotations

import json
import os
import tempfile
import threading


_LOCK = threading.RLock()


def _projects_location() -> str:
    from .project_directory import ProjectDirectory
    return os.path.realpath(os.path.expanduser(ProjectDirectory.base_location()))


def _metadata_path() -> str:
    return os.path.join(_projects_location(), ".catalystlab", "stage-overrides.json")


def _key(project_path: str, stage_id: str) -> str:
    projects = _projects_location()
    project = os.path.realpath(os.path.expanduser(project_path))
    try:
        key = os.path.relpath(project, projects)
        if key == os.pardir or key.startswith(os.pardir + os.sep):
            key = project
    except ValueError:
        key = project
    return f"{key}::{stage_id}"


def _load() -> dict:
    try:
        with open(_metadata_path(), encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Failed to load stage override metadata: {error}")
        return {}


def _save(data: dict):
    path = _metadata_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".stage-overrides-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, sort_keys=True)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def get(project_path: str, stage_id: str) -> dict:
    with _LOCK:
        item = _load().get(_key(project_path, stage_id), {})
        return item if isinstance(item, dict) else {}


def set_defaults(project_path: str, stage_id: str, defaults: dict[str, dict]):
    """Record defaults created with a stage, for distinguishing them from explicit edits later."""
    if not defaults:
        return
    with _LOCK:
        data = _load()
        key = _key(project_path, stage_id)
        item = dict(data.get(key, {}))
        current = dict(item.get("defaults", {}))
        current.update(defaults)
        item["defaults"] = current
        item.setdefault("explicit", [])
        data[key] = item
        _save(data)


def checkpoint_committed(project_path: str):
    """Remember explicit markers alongside the current committed project files."""
    prefix = _key(project_path, "")
    with _LOCK:
        data = _load()
        changed = False
        for key, item in data.items():
            if key.startswith(prefix) and isinstance(item, dict):
                item["committed_explicit"] = sorted(set(item.get("explicit", [])))
                changed = True
        if changed:
            _save(data)


def restore_committed(project_path: str):
    """Restore the explicit markers associated with the checked-out project files.

    Older Library metadata has no checkpoint. In that case clear its explicit markers:
    after a Git discard they cannot be distinguished from edits that were just discarded.
    """
    prefix = _key(project_path, "")
    with _LOCK:
        data = _load()
        changed = False
        for key, item in list(data.items()):
            if not key.startswith(prefix) or not isinstance(item, dict):
                continue
            item["explicit"] = sorted(set(item.get("committed_explicit", [])))
            item.setdefault("committed_explicit", [])
            if not item["explicit"] and not item.get("defaults"):
                data.pop(key, None)
            changed = True
        if changed:
            _save(data)


def mark_explicit(project_path: str, stage_id: str, argument: str, explicit: bool = True):
    """Record a user edit even when it equals the value currently supplied by a default."""
    with _LOCK:
        data = _load()
        key = _key(project_path, stage_id)
        item = dict(data.get(key, {}))
        values = set(item.get("explicit", []))
        values.add(argument) if explicit else values.discard(argument)
        item["explicit"] = sorted(values)
        item.setdefault("defaults", {})
        if item["explicit"] or item["defaults"]:
            data[key] = item
        else:
            data.pop(key, None)
        _save(data)

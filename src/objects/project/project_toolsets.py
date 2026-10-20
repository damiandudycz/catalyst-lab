"""Library-local preference for the Catalyst toolset selected for a project."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from .json_serialization import dump_json


_LOCK = threading.RLock()


def _projects_location() -> str:
    from .project_directory import ProjectDirectory
    return os.path.realpath(os.path.expanduser(ProjectDirectory.base_location()))


def _project_key(project_path: str) -> str:
    projects = _projects_location()
    project = os.path.realpath(os.path.expanduser(project_path))
    try:
        relative = os.path.relpath(project, projects)
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return project
        return relative
    except ValueError:
        return project


def _metadata_path() -> str:
    return os.path.join(_projects_location(), ".catalystlab", "project-toolsets.json")


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as file:
            values = json.load(file)
        return values if isinstance(values, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Failed to load project toolset selections: {error}")
        return {}


def _legacy_selection(project_path: str) -> str | None:
    """Migrate an old per-stage choice when every stage agreed on one toolset."""
    project_key = _project_key(project_path)
    prefix = project_key + "::"
    values = _read(os.path.join(_projects_location(), ".catalystlab", "stage-toolsets.json"))
    selections = {str(value) for key, value in values.items() if key.startswith(prefix) and value}
    return next(iter(selections)) if len(selections) == 1 else None


def get(project_path: str) -> str | None:
    with _LOCK:
        value = _read(_metadata_path()).get(_project_key(project_path))
        if value:
            return str(value)
        legacy = _legacy_selection(project_path)
        if legacy:
            _write(project_path, legacy)
        return legacy


def _write(project_path: str, toolset_id: str | None):
    path = _metadata_path()
    values = _read(path)
    key = _project_key(project_path)
    if toolset_id:
        values[key] = str(toolset_id)
    else:
        values.pop(key, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".project-toolsets-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            dump_json(file, values)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def set(project_path: str, toolset_id: str | None):
    with _LOCK:
        _write(project_path, toolset_id)

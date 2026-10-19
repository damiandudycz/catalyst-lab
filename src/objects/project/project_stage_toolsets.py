"""Library-local preferences for the Catalyst toolset selected per project stage."""
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


def _metadata_path() -> str:
    return os.path.join(_projects_location(), ".catalystlab", "stage-toolsets.json")


def _key(project_path: str, stage_id: str) -> str:
    projects = _projects_location()
    project = os.path.realpath(os.path.expanduser(project_path))
    try:
        project_key = os.path.relpath(project, projects)
        if project_key == os.pardir or project_key.startswith(os.pardir + os.sep):
            project_key = project
    except ValueError:
        project_key = project
    return f"{project_key}::{stage_id}"


def get(project_path: str, stage_id: str) -> str | None:
    with _LOCK:
        try:
            with open(_metadata_path(), encoding="utf-8") as file:
                values = json.load(file)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            print(f"Failed to load stage toolset selections: {error}")
            return None
        value = values.get(_key(project_path, stage_id))
        return str(value) if value else None


def set(project_path: str, stage_id: str, toolset_id: str | None):
    with _LOCK:
        path = _metadata_path()
        try:
            with open(path, encoding="utf-8") as file:
                values = json.load(file)
            if not isinstance(values, dict):
                values = {}
        except FileNotFoundError:
            values = {}
        except (OSError, json.JSONDecodeError) as error:
            print(f"Failed to load stage toolset selections: {error}")
            values = {}

        key = _key(project_path, stage_id)
        if toolset_id:
            values[key] = str(toolset_id)
        else:
            values.pop(key, None)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(prefix=".stage-toolsets-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                dump_json(file, values)
                file.write("\n")
            os.replace(temporary_path, path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

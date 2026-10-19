"""Visible per-stage snapshots captured when the stage creation wizard completes."""
import json
import os
import uuid
from typing import Any
from .json_serialization import dump_json


STAGE_BASELINE_FILE = "stage-baseline.json"


def _baseline_path(stage_directory: str) -> str:
    return os.path.join(stage_directory, STAGE_BASELINE_FILE)


def stage_creation_baseline(project_path: str, stage_id: uuid.UUID | str) -> dict[str, Any]:
    stages_path = os.path.join(project_path, "stages")
    try:
        stage_directories = os.scandir(stages_path)
    except OSError:
        return {}
    with stage_directories as directories:
        for entry in directories:
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                with open(os.path.join(entry.path, "stage.json"), encoding="utf-8") as file:
                    stage = json.load(file)
                identifier = stage.get("id", {}).get("value")
                if str(identifier) != str(stage_id):
                    continue
                with open(_baseline_path(entry.path), encoding="utf-8") as file:
                    baseline = json.load(file)
                if isinstance(baseline, dict) and baseline:
                    return {**baseline, "__catalystlab_baseline_kind__": "initial_snapshot"}
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    return {}


def record_stage_creation_baseline(stage_directory: str, serialized_stage: dict[str, Any]) -> None:
    with open(_baseline_path(stage_directory), "w", encoding="utf-8") as file:
        dump_json(file, serialized_stage)


def remove_stage_creation_baseline(stage_directory: str) -> None:
    try:
        os.remove(_baseline_path(stage_directory))
    except FileNotFoundError:
        pass

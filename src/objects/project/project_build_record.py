from __future__ import annotations
import json, os
from .multistage_process import MultiStageProcess, MultiStageProcessStage, MultiStageProcessState, MultiStageProcessStageState
from .project_build import project_builds_directory, stage_builds_directory, StageBuildStatus

# ------------------------------------------------------------------------------
# Record of build run (steps with their results), saved when run finishes, so its steps can be shown like build
# progress later (also after app restart). Output of steps building stages is in build.log of their builds (only
# reference is saved), other steps (eg. preparing toolset, getting snapshot) keep their output in record.
# Runs made before records were saved are shown from builds of their stages.

RECORD_FORMAT = 1
_MAX_RECORDED_LINES = 2_000 # Output of other steps, last lines.
_MAX_LOG_LINES = 5_000 # Shown lines of build.log, last lines (whole log is in build folder).

def build_records_directory(project_directory) -> str:
    return os.path.join(project_builds_directory(project_directory), "runs")

def _record_path(project_directory, timestamp: str) -> str:
    return os.path.join(build_records_directory(project_directory), f"{timestamp}.json")

def save_build_record(process):
    """Saves steps of build run (ProjectBuild). Saved while it runs too (when steps change), so runs interrupted by
    closing app can be shown."""
    from .project_build_process import ProjectBuildStepBuildStage
    steps = []
    for step in process.stages:
        data = {"name": step.name, "description": step.description, "state": step.state.name, "progress": step.progress}
        if isinstance(step, ProjectBuildStepBuildStage):
            data["stage_name"] = step.stage.name
            data["stage_id"] = str(step.stage.id)
            data["started"] = step.build is not None # Output is in build.log of stage build.
        else:
            data["output"] = step.output_lines[-_MAX_RECORDED_LINES:]
        steps.append(data)
    stopped = getattr(process, "canceled", False)
    from .multistage_process import MultiStageProcessState as State
    status = "stopped" if stopped else "interrupted" if process.status == State.IN_PROGRESS else process.status.name.lower()
    record = {
        "format": RECORD_FORMAT,
        "timestamp": process.timestamp,
        "title": process.title,
        "status": status,
        "steps": steps,
    }
    path = _record_path(process.project_directory, process.timestamp)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(record, file, indent=2)
    except OSError as e:
        print(f"Failed to save record of build {process.timestamp}: {e}")

def failed_steps(project_directory, timestamp: str) -> list[str]:
    """Names of failed steps of run that don't build stages (eg. seed download), stages depending on them were
    skipped. Empty for runs without record."""
    try:
        with open(_record_path(project_directory, timestamp), encoding="utf-8") as file:
            record = json.load(file)
    except (OSError, ValueError):
        return []
    return [step["name"] for step in record.get("steps", [])
            if "stage_name" not in step and step.get("state") == MultiStageProcessStageState.FAILED.name]

def delete_build_record(project_directory, timestamp: str):
    try:
        os.remove(_record_path(project_directory, timestamp))
    except OSError:
        pass

def load_build_record(project_directory, timestamp: str, builds: list) -> RecordedBuild:
    """Finished run with given timestamp, from its record, or from builds of its stages (runs without record)."""
    try:
        with open(_record_path(project_directory, timestamp), encoding="utf-8") as file:
            record = json.load(file)
        steps = []
        for data in record.get("steps", []):
            output = data.get("output")
            if output is None:
                stage_id = data.get("stage_id")
                if stage_id is None:
                    stage_id = next((str(build.stage_id) for build in builds
                                     if build.timestamp == timestamp and build.stage_name == data.get("stage_name")), None)
                output = _stage_log(project_directory, stage_id, timestamp) if data.get("started") and stage_id else []
            steps.append((data["name"], data.get("description", ""), _step_state(data.get("state")), output, data.get("progress")))
        return RecordedBuild(project_directory, record.get("title") or f"Building {project_directory.name}",
                             record.get("status"), steps)
    except (OSError, ValueError, KeyError):
        pass
    ordered = sorted(builds, key=lambda build: (build.order is None, build.order or 0, build.date))
    steps = []
    for build in ordered:
        started = build.status.is_attempt
        state = (MultiStageProcessStageState.COMPLETED if build.status == StageBuildStatus.COMPLETED
                 else MultiStageProcessStageState.FAILED if started else MultiStageProcessStageState.SCHEDULED)
        output = _stage_log(project_directory, str(build.stage_id), timestamp) if started else []
        steps.append((f"Build {build.stage_name}", "Builds stage with catalyst", state, output, None))
    stopped = any(build.status in (StageBuildStatus.STOPPED, StageBuildStatus.CANCELED) for build in builds)
    failed = any(step[2] == MultiStageProcessStageState.FAILED for step in steps)
    status = "stopped" if stopped else "failed" if failed else "completed"
    return RecordedBuild(project_directory, f"Building {project_directory.name}", status, steps)

def _step_state(name: str | None) -> MultiStageProcessStageState:
    state = MultiStageProcessStageState.__members__.get(name or "", MultiStageProcessStageState.SCHEDULED)
    # Step running when record was saved was interrupted.
    return MultiStageProcessStageState.FAILED if state == MultiStageProcessStageState.IN_PROGRESS else state

def _stage_log(project_directory, stage_id: str | None, timestamp: str) -> list[str]:
    if not stage_id:
        return []
    import uuid
    path = os.path.join(stage_builds_directory(project_directory, uuid.UUID(stage_id)), timestamp, "build.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError:
        return ["Build log is not available."]
    if len(lines) > _MAX_LOG_LINES:
        return [f"… {len(lines) - _MAX_LOG_LINES} earlier lines are in {path}"] + lines[-_MAX_LOG_LINES:]
    return lines

class RecordedBuildStep(MultiStageProcessStage):
    """Step of finished build, with its saved result and output."""

    def __init__(self, name: str, description: str, state: MultiStageProcessStageState, output: list[str],
                 progress: float | None, process):
        super().__init__(name=name, description=description, multistage_process=process)
        self.state = state
        self.output_lines = list(output)
        self.progress = 1.0 if state == MultiStageProcessStageState.COMPLETED else progress

    def start(self):
        pass

class RecordedBuild(MultiStageProcess):
    """Finished build run shown like build progress. It's not started, nothing runs."""

    def __init__(self, project_directory, title: str, status: str | None, steps: list[tuple]):
        self.project_directory = project_directory
        self._steps = steps
        super().__init__(title=title)
        self.canceled = status == "stopped"
        self.status = MultiStageProcessState.COMPLETED if status == "completed" else MultiStageProcessState.FAILED
        self.progress = sum(step.progress or 0 for step in self.stages) / len(self.stages) if self.stages else 1.0

    def name(self) -> str:
        return self.project_directory.name

    def setup_stages(self):
        self.stages = [RecordedBuildStep(name, description, state, output, progress, self)
                       for name, description, state, output, progress in self._steps]

    def complete_process(self, success: bool):
        pass

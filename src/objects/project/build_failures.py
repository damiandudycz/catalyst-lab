from __future__ import annotations
import hashlib, os, re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

# ------------------------------------------------------------------------------
# Packages that failed to build in stages of projects (Bugs section), from failure details of stage builds.

_VERSION = re.compile(r"-\d[^-/]*(-r\d+)?$")
_RESOLUTION_STATES = ("open", "solved", "potentially_solved", "potentially_resurfaced")

def _resolution_status(item: dict) -> str:
    status = item.get("resolution")
    return status if status in _RESOLUTION_STATES else "solved" if item.get("fixed") else "open"

def package_atom(package: str) -> str:
    """Category and name of package with version (sys-libs/readline-8.3_p6 -> sys-libs/readline)."""
    return _VERSION.sub("", package)

def bugzilla_url(atom: str) -> str:
    """Bugs of package in Gentoo Bugzilla (open ones, searched by category/name)."""
    return f"https://bugs.gentoo.org/buglist.cgi?quicksearch={quote(atom)}"

@dataclass
class PackageFailure:
    package: str              # With version, eg. sys-libs/readline-8.3_p6.
    repository: str           # Repository of package (gentoo or overlay).
    phase: str | None         # Ebuild phase that failed (configure, compile...).
    reason: str | None        # Known reason found in output (eg. out of memory).
    project_directory: object
    build: object             # StageBuild.
    index: int                # Position in failed packages of build (its state is saved there).

    def _item(self) -> dict:
        return self.build.details["failure"]["packages"][self.index]

    @property
    def fixed(self) -> bool:
        return self.resolution_status == "solved"

    @property
    def resolution_status(self) -> str:
        return _resolution_status(self._item())

    def set_fixed(self, fixed: bool):
        """Marks failure as fixed (or not), saved in build."""
        item = self._item()
        item["fixed"] = fixed
        item["resolution"] = "solved" if fixed else "open"
        self.build.save(self.project_directory)

    def remove(self):
        """Removes failure from Bugs (build and its logs stay), saved in build."""
        self._item()["removed"] = True
        self.build.save(self.project_directory)

    @property
    def atom(self) -> str:
        return package_atom(self.package)

    @property
    def issue_key(self) -> tuple | None:
        """Stable key for repeated, confidently identified failures in one project stage.

        Package versions are omitted so the same failure can be recognized after an
        update. Repository and phase remain part of the key to avoid merging failures
        that merely share a package name or broad reason.
        """
        reason = " ".join((self.reason or "").casefold().split())
        if not reason:
            return None
        project_id = getattr(self.project_directory, "id", None)
        stage_id = getattr(self.build, "stage_id", None)
        if project_id is None or stage_id is None:
            return None
        return (str(project_id), str(stage_id), self.atom,
                self.repository.casefold(), (self.phase or "").casefold(), reason)

    @property
    def version(self) -> str:
        return self.package[len(self.atom) + 1:]

    @property
    def date(self) -> datetime:
        return self.build.date

    @property
    def is_gentoo(self) -> bool:
        """Package of Gentoo repository (its bugs are in Gentoo Bugzilla)."""
        return self.repository == "gentoo"

    @property
    def failure_directory(self) -> str | None:
        from .project_build import StageBuild
        path = os.path.join(self.build.path, StageBuild.FAILURE_DIRECTORY) if self.build.path else None
        return path if path and os.path.isdir(path) else None

    @property
    def log_path(self) -> str | None:
        """Most useful log of failure: summary of collected logs, build.log of package, or log of stage build."""
        candidates = []
        if directory := self.failure_directory:
            candidates += [os.path.join(directory, "summary.txt"),
                           os.path.join(directory, self.package.replace("/", "_"), "build.log")]
        if self.build.path:
            candidates.append(os.path.join(self.build.path, "build.log"))
        return next((path for path in candidates if os.path.isfile(path)), None)

def collect_package_failures() -> list[PackageFailure]:
    """Failed packages of all projects, newest first."""
    from .repository import Repository
    from .project_build import load_project_builds, StageBuildStatus
    failures = []
    for project in Repository.ProjectDirectory.value:
        try:
            builds = load_project_builds(project)
        except Exception as e:
            print(f"Failed to read builds of {project.name}: {e}")
            continue
        _reconcile_resolution_states(project, builds)
        for build in builds:
            if build.status != StageBuildStatus.FAILED:
                continue
            failure = (build.details or {}).get("failure") or {}
            for index, item in enumerate(failure.get("packages") or []):
                if (package := item.get("package")) and not item.get("removed"):
                    failures.append(PackageFailure(package=package, repository=item["repository"], phase=item["phase"],
                                                   reason=failure.get("reason"), project_directory=project, build=build,
                                                   index=index))
    failures.sort(key=lambda failure: failure.date, reverse=True)
    return failures


def reconcile_project_failure_states(project):
    """Reconcile automatic resolution states across the project's build history."""
    from .project_build import load_project_builds
    try:
        _reconcile_resolution_states(project, load_project_builds(project))
    except Exception as error:
        print(f"Failed to update automatic bug states for {project.name}: {error}")


def _configuration_key(build) -> str | None:
    """Fingerprint stage settings without overlay contents, which may contain the fix."""
    from .project_build import StageBuild
    if not build.path:
        return None
    spec_path = os.path.join(build.path, StageBuild.SPEC_FILE)
    try:
        with open(spec_path, encoding="utf-8") as file:
            spec = file.read()
    except OSError:
        return None
    inputs = build.details.get("inputs") or {}
    toolset = inputs.get("toolset") or {}
    try:
        return stage_configuration_key(spec, build.timestamp, toolset.get("catalyst"))
    except (OSError, TypeError):
        return None


def stage_configuration_key(spec: str, timestamp: str, catalyst_version: str | None) -> str:
    """Fingerprint stage settings while allowing overlay contents to change as fixes."""
    digest = hashlib.sha256()
    for line in spec.splitlines():
        # The seed archive changes on every parent rebuild, but stage settings are
        # represented by the other generated spec values.
        # Snapshot revisions and the per-project kernel cache path do not define
        # the failure configuration: a snapshot can change while an overlay fix
        # is being tested, and cache paths are storage locations rather than inputs.
        key = line.partition(":")[0]
        if key in ("source_subpath", "snapshot_treeish", "kerncache_path"):
            continue
        digest.update(line.replace(timestamp, "<build>").encode())
        digest.update(b"\n")
    digest.update(str(catalyst_version or "").encode())
    return digest.hexdigest()


def _reconcile_resolution_states(project, builds: list):
    """Infer potential fixes and recurrences from later builds of identical configuration."""
    from .project_build import StageBuildStatus
    histories: dict[tuple, list[tuple[object, dict]]] = {}
    changed: dict[str, object] = {}
    for build in sorted(builds, key=lambda item: (item.date, str(item.id))):
        config = _configuration_key(build)
        stage = str(build.stage_id)
        process_event = not (build.details or {}).get("bug_resolution_processed")
        if build.status == StageBuildStatus.COMPLETED:
            if process_event and config is not None:
                for (history_stage, history_config, _), occurrences in histories.items():
                    if (history_stage, history_config) != (stage, config):
                        continue
                    for old_build, item in occurrences:
                        status = _resolution_status(item)
                        if status in ("open", "potentially_resurfaced"):
                            item["resolution"] = "potentially_solved"
                            changed[str(old_build.id)] = old_build
            if process_event:
                build.details["bug_resolution_processed"] = True
                changed[str(build.id)] = build
            continue
        if build.status != StageBuildStatus.FAILED:
            continue
        failure = (build.details or {}).get("failure") or {}
        for index, item in enumerate(failure.get("packages") or []):
            if not item.get("package"):
                continue
            current = PackageFailure(package=item["package"], repository=item["repository"],
                                     phase=item["phase"], reason=failure.get("reason"),
                                     project_directory=project, build=build, index=index)
            issue = current.issue_key
            if issue is None or config is None:
                continue
            key = (stage, config, issue)
            if process_event:
                for old_build, old_item in histories.get(key, []):
                    status = _resolution_status(old_item)
                    if status == "potentially_solved":
                        old_item["resolution"] = "open"
                        old_item["fixed"] = False
                        changed[str(old_build.id)] = old_build
                    elif status == "solved":
                        old_item["resolution"] = "potentially_resurfaced"
                        changed[str(old_build.id)] = old_build
                item.setdefault("resolution", "solved" if item.get("fixed") else "open")
            histories.setdefault(key, []).append((build, item))
        if process_event:
            build.details["bug_resolution_processed"] = True
            changed[str(build.id)] = build
    for build in changed.values():
        try:
            build.save(project)
        except OSError as error:
            print(f"Failed to save automatic bug status for {build.stage_name}: {error}")

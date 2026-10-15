from __future__ import annotations
import os, re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

# ------------------------------------------------------------------------------
# Packages that failed to build in stages of projects (Bugs section), from failure details of stage builds.

_VERSION = re.compile(r"-\d[^-/]*(-r\d+)?$")

def package_atom(package: str) -> str:
    """Category and name of package with version (sys-libs/readline-8.3_p6 -> sys-libs/readline)."""
    return _VERSION.sub("", package)

def bugzilla_url(atom: str) -> str:
    """Bugs of package in Gentoo Bugzilla (open ones, searched by category/name)."""
    return f"https://bugs.gentoo.org/buglist.cgi?quicksearch={quote(atom)}"

@dataclass
class PackageFailure:
    package: str              # With version, eg. sys-libs/readline-8.3_p6.
    repository: str | None    # Repository of package (gentoo or overlay), None in records made before it was saved.
    phase: str | None         # Ebuild phase that failed (configure, compile...).
    reason: str | None        # Known reason found in output (eg. out of memory).
    project_directory: object
    build: object             # StageBuild.
    index: int                # Position in failed packages of build (its state is saved there).

    def _item(self) -> dict:
        return self.build.details["failure"]["packages"][self.index]

    @property
    def fixed(self) -> bool:
        return bool(self._item().get("fixed"))

    def set_fixed(self, fixed: bool):
        """Marks failure as fixed (or not), saved in build."""
        self._item()["fixed"] = fixed
        self.build.save(self.project_directory)

    def remove(self):
        """Removes failure from Bugs (build and its logs stay), saved in build."""
        self._item()["removed"] = True
        self.build.save(self.project_directory)

    @property
    def atom(self) -> str:
        return package_atom(self.package)

    @property
    def version(self) -> str:
        return self.package[len(self.atom) + 1:]

    @property
    def date(self) -> datetime:
        return self.build.date

    @property
    def is_gentoo(self) -> bool:
        """Package of Gentoo repository (its bugs are in Gentoo Bugzilla), assumed for records without repository."""
        return self.repository in (None, "gentoo")

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
        for build in builds:
            if build.status != StageBuildStatus.FAILED:
                continue
            failure = (build.details or {}).get("failure") or {}
            for index, item in enumerate(failure.get("packages") or []):
                if (package := item.get("package")) and not item.get("removed"):
                    failures.append(PackageFailure(package=package, repository=item.get("repository"), phase=item.get("phase"),
                                                   reason=failure.get("reason"), project_directory=project, build=build,
                                                   index=index))
    failures.sort(key=lambda failure: failure.date, reverse=True)
    return failures

from __future__ import annotations
import os, re
from typing import final
from .git_directory import GitDirectory
from .architecture import Architecture
from .snapshot import PortageProfile, parse_profiles_desc

@final
class OverlayDirectory(GitDirectory):

    @classmethod
    def base_location(cls) -> str:
        from .repository import Repository
        import os
        return os.path.realpath(
            os.path.expanduser(
                Repository.Settings.value.overlay_location
            )
        )


    def repo_name(self) -> str:
        """Repository name used by portage, from profiles/repo_name or metadata/layout.conf (repo-name)."""
        directory = self.directory_path()
        try:
            with open(os.path.join(directory, "profiles", "repo_name"), encoding="utf-8") as file:
                if name := file.readline().strip():
                    return name
        except OSError:
            pass
        try:
            with open(os.path.join(directory, "metadata", "layout.conf"), encoding="utf-8") as file:
                for line in file:
                    if match := re.match(r"^\s*repo-name\s*=\s*(\S+)", line):
                        return match.group(1)
        except OSError:
            pass
        return self.name

    def load_ebuilds(self) -> dict[str, dict[str, list[str]]]:
        """Packages of overlay by categories, with versions of their ebuilds (like Snapshot.load_ebuilds). Categories are
        folders with packages (category/package/package-version.ebuild), sorted."""
        directory = self.directory_path()
        result: dict[str, dict[str, list[str]]] = {}
        try:
            categories = sorted(entry.name for entry in os.scandir(directory) if entry.is_dir() and not entry.name.startswith("."))
        except OSError:
            return result
        for category in categories:
            packages = {}
            for package in sorted(os.listdir(os.path.join(directory, category))):
                package_directory = os.path.join(directory, category, package)
                if not os.path.isdir(package_directory):
                    continue
                versions = sorted((file[len(package) + 1:-len(".ebuild")] for file in os.listdir(package_directory)
                                   if file.startswith(package + "-") and file.endswith(".ebuild")),
                                  key=lambda version: [int(part) for part in re.findall(r"\d+", version)])
                if versions:
                    packages[package] = versions
            if packages:
                result[category] = packages
        return result

    def load_profiles(self, arch: Architecture) -> list[PortageProfile]:
        """Profiles for given arch listed in profiles/profiles.desc of overlay. Empty if overlay doesn't define any."""
        try:
            with open(os.path.join(self.directory_path(), "profiles", "profiles.desc"), encoding="utf-8") as file:
                return parse_profiles_desc(contents=file.read(), arch=arch, repo=self.repo_name())
        except OSError:
            return []

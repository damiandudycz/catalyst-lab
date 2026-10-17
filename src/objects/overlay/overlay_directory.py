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
        from .repository import Repository, resolve_path
        return resolve_path(Repository.Settings.value.overlay_location)


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
        """Packages of overlay by category, with sorted ebuild versions."""
        from .ebuilds import group_ebuild_paths
        directory = self.directory_path()
        paths = (f"{category.name}/{package.name}/{ebuild.name}"
                 for category in os.scandir(directory) if category.is_dir() and not category.name.startswith(".")
                 for package in os.scandir(category.path) if package.is_dir()
                 for ebuild in os.scandir(package.path) if ebuild.is_file() and ebuild.name.endswith(".ebuild"))
        return group_ebuild_paths(paths)

    def load_profiles(self, arch: Architecture) -> list[PortageProfile]:
        """Profiles for given arch listed in profiles/profiles.desc of overlay. Empty if overlay doesn't define any."""
        try:
            with open(os.path.join(self.directory_path(), "profiles", "profiles.desc"), encoding="utf-8") as file:
                return parse_profiles_desc(contents=file.read(), arch=arch, repo=self.repo_name())
        except OSError:
            return []

from __future__ import annotations
import os, re, shutil, subprocess, tempfile, threading
from dataclasses import dataclass, field
from enum import Enum

# ------------------------------------------------------------------------------
# Kernel packages offered for kernels of stages (boot/kernel/<name>/sources): sys-kernel packages of snapshot of
# project and of overlays used by stage. Catalyst builds kernel from sources of package (/usr/src/linux-*), so packages
# are recognized by eclasses their ebuilds inherit (conventions of Gentoo, not names): distribution kernels build
# themselves (kernel-build, kernel-install), sources are compiled by genkernel (kernel-2). Other packages (eg. prebuilt
# raspberrypi-image) don't have sources, they are installed as packages of stage instead.

class KernelPackageKind(Enum):
    DISTRIBUTION = "distribution" # Distribution kernel (distkernel: yes).
    SOURCES = "sources"           # Kernel sources, compiled by genkernel.
    UNSUPPORTED = "unsupported"   # No kernel sources for catalyst.

    @property
    def description(self) -> str:
        return {
            KernelPackageKind.DISTRIBUTION: "Distribution kernel",
            KernelPackageKind.SOURCES: "Kernel sources, compiled with genkernel",
            KernelPackageKind.UNSUPPORTED: "Not kernel sources, install it as package of stage",
        }[self]

_DISTRIBUTION_ECLASSES = {"kernel-build", "kernel-install"}
_SOURCES_ECLASSES = {"kernel-2"}
_INHERIT = re.compile(r"^\s*inherit\s+([^#\n]*)", re.MULTILINE)
_HEADERS = re.compile(r"^\s*ETYPE=[\"']?headers", re.MULTILINE)

def kernel_package_kind(ebuild: str) -> KernelPackageKind:
    """Kind of package from inherit lines of its ebuild."""
    eclasses = {eclass for line in _INHERIT.findall(ebuild) for eclass in line.split()}
    if eclasses & _DISTRIBUTION_ECLASSES:
        return KernelPackageKind.DISTRIBUTION
    # kernel-2 is used also by kernel headers (ETYPE="headers"), only sources can be built.
    if eclasses & _SOURCES_ECLASSES and not _HEADERS.search(ebuild):
        return KernelPackageKind.SOURCES
    return KernelPackageKind.UNSUPPORTED

@dataclass
class KernelPackage:
    atom: str # category/package
    kind: KernelPackageKind
    repositories: list[str] = field(default_factory=list) # gentoo (snapshot) and overlay names.

    @property
    def supported(self) -> bool:
        return self.kind != KernelPackageKind.UNSUPPORTED

    @property
    def details(self) -> str:
        return f"{self.kind.description} · {', '.join(self.repositories)}"

_CATEGORY = "sys-kernel"
_cache: dict[tuple, dict[str, KernelPackageKind]] = {}
_cache_lock = threading.Lock()

def load_kernel_packages(project_directory, stage) -> list[KernelPackage]:
    """Kernel packages of snapshot of project and overlays used by stage. Slow at first (snapshot is read), call it in
    background. Packages with kernel sources first."""
    packages: dict[str, KernelPackage] = {}
    sources = []
    if (snapshot := project_directory.get_snapshot()) is not None and os.path.isfile(snapshot.file_path()):
        sources.append(("gentoo", ("snapshot", snapshot.filename), lambda snapshot=snapshot: _snapshot_packages(snapshot.file_path())))
    for name, path in _stage_overlays(project_directory, stage):
        sources.append((name, ("overlay", path, _tree_state(path)), lambda path=path: _folder_packages(path)))
    for repository, key, load in sources:
        with _cache_lock:
            kinds = _cache.get(key)
        if kinds is None:
            try:
                kinds = load()
            except Exception as e:
                print(f"Failed to read kernel packages of {repository}: {e}")
                continue
            with _cache_lock:
                _cache[key] = kinds
        for atom, kind in kinds.items():
            package = packages.setdefault(atom, KernelPackage(atom=atom, kind=kind))
            if kind != KernelPackageKind.UNSUPPORTED:
                package.kind = kind # Overlay can provide kernel with the same name.
            package.repositories.append(repository)
    return sorted(packages.values(), key=lambda package: (not package.supported, package.atom))

def package_kind(atom: str | None, packages: list[KernelPackage]) -> KernelPackageKind | None:
    """Kind of package given as Portage atom (eg. =sys-kernel/gentoo-kernel-6.12.8:6.12::gentoo), None when it's not
    one of packages (not in snapshot or overlays of stage)."""
    if not atom:
        return None
    name = re.sub(r"^[<>=~!]+", "", atom.strip()).split("::")[0].split(":")[0]
    known = {package.atom: package.kind for package in packages}
    if name in known:
        return known[name]
    # Versioned atom: package name followed by -version.
    matches = [package for package in known if name.startswith(package + "-") and name[len(package) + 1:][:1].isdigit()]
    return known[max(matches, key=len)] if matches else None

def derived_distkernel(atom: str | None, packages: list[KernelPackage]) -> bool | None:
    """Distribution kernel setting matching package: catalyst can build distribution kernels only as distribution
    kernels and sources only with genkernel. None when kind of package isn't known."""
    match package_kind(atom, packages):
        case KernelPackageKind.DISTRIBUTION: return True
        case KernelPackageKind.SOURCES: return False
    return None

# Distribution kernel catalyst uses when kernel doesn't set package (and releng templates use).
DEFAULT_KERNEL_PACKAGE = "sys-kernel/gentoo-kernel"

def default_kernel_package(project_directory, stage) -> str:
    """Package of added kernel: distribution kernel of overlay used by stage (made for its hardware, eg. PS3), or
    distribution kernel of Gentoo."""
    try:
        for package in load_kernel_packages(project_directory, stage):
            if package.kind == KernelPackageKind.DISTRIBUTION and any(repository != "gentoo" for repository in package.repositories):
                return package.atom
    except Exception as e:
        print(f"Failed to read kernel packages: {e}")
    return DEFAULT_KERNEL_PACKAGE

def _stage_overlays(project_directory, stage) -> list[tuple[str, str]]:
    """Overlays used by stage (repos, own or inherited): names and folders."""
    from .project_stage_arguments import StageArgumentDetails
    from .project_stage_value_resolver import resolve_stage_argument
    from .repository import Repository
    import uuid
    value = resolve_stage_argument(project_directory, stage, StageArgumentDetails.repos.value)
    overlays = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, uuid.UUID):
            if overlay := next((overlay for overlay in Repository.OverlayDirectory.value if overlay.id == item), None):
                overlays.append((overlay.name, overlay.directory_path()))
        elif isinstance(item, str) and os.path.isdir(item):
            overlays.append((os.path.basename(item.rstrip("/")), item))
    return overlays

def _tree_state(path: str) -> float:
    """Changes when packages of folder change (cache of overlay is read again)."""
    category = os.path.join(path, _CATEGORY)
    try:
        return max([os.path.getmtime(category)] + [os.path.getmtime(entry.path) for entry in os.scandir(category)])
    except OSError:
        return 0

def _newest_ebuilds(names: list[str]) -> dict[str, str]:
    """Ebuild with the newest version of every package, from paths category/package/package-version.ebuild."""
    newest: dict[str, tuple] = {}
    for name in names:
        parts = name.split("/")
        if len(parts) != 3 or not parts[2].endswith(".ebuild"):
            continue
        package = parts[1]
        version = parts[2][len(package) + 1:-len(".ebuild")]
        key = tuple(int(number) for number in re.findall(r"\d+", version))
        atom = f"{parts[0]}/{package}"
        if atom not in newest or key > newest[atom][0]:
            newest[atom] = (key, name)
    return {atom: name for atom, (key, name) in newest.items()}

def _folder_packages(path: str) -> dict[str, KernelPackageKind]:
    category = os.path.join(path, _CATEGORY)
    if not os.path.isdir(category):
        return {}
    names = [f"{_CATEGORY}/{package}/{file}" for package in os.listdir(category)
             if os.path.isdir(os.path.join(category, package)) for file in os.listdir(os.path.join(category, package))]
    kinds = {}
    for atom, name in _newest_ebuilds(names).items():
        with open(os.path.join(path, name), encoding="utf-8", errors="replace") as file:
            kinds[atom] = kernel_package_kind(file.read())
    return kinds

def _snapshot_packages(snapshot_path: str) -> dict[str, KernelPackageKind]:
    """Ebuilds of snapshot (.sqfs) are listed, newest ebuild of every package is extracted (all in one run)."""
    listing = subprocess.run(["unsquashfs", "-l", snapshot_path, _CATEGORY], capture_output=True, text=True, check=True).stdout
    names = [line.strip().removeprefix("squashfs-root/") for line in listing.splitlines() if line.strip().endswith(".ebuild")]
    ebuilds = _newest_ebuilds(names)
    if not ebuilds:
        return {}
    directory = tempfile.mkdtemp(prefix="catalystlab-kernels-")
    try:
        files = os.path.join(directory, "files")
        with open(files, "w", encoding="utf-8") as file:
            file.write("\n".join(ebuilds.values()) + "\n")
        subprocess.run(["unsquashfs", "-n", "-d", os.path.join(directory, "root"), "-ef", files, snapshot_path],
                       capture_output=True, check=True)
        kinds = {}
        for atom, name in ebuilds.items():
            try:
                with open(os.path.join(directory, "root", name), encoding="utf-8", errors="replace") as file:
                    kinds[atom] = kernel_package_kind(file.read())
            except OSError:
                kinds[atom] = KernelPackageKind.UNSUPPORTED
        return kinds
    finally:
        shutil.rmtree(directory, ignore_errors=True)

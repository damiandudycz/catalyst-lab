from .repository import Repository

# ------------------------------------------------------------------------------
# Registered global repositories:

from .toolset import Toolset
from .snapshot import Snapshot
from .releng_directory import RelengDirectory
from .overlay_directory import OverlayDirectory
from .project_directory import ProjectDirectory
from .settings import Settings
from .build_machine import BuildMachine
from .packages_directory import PackagesDirectory

# Settings define the Library roots needed by each object repository's load_all().
Repository.Settings = Repository(cls=Settings, default_factory=Settings)
Repository.Toolset = Repository(cls=Toolset, collection=True)
Repository.Snapshot = Repository(cls=Snapshot, collection=True)
Repository.RelengDirectory = Repository(cls=RelengDirectory, collection=True)
Repository.OverlayDirectory = Repository(cls=OverlayDirectory, collection=True)
Repository.ProjectDirectory = Repository(cls=ProjectDirectory, collection=True)
Repository.BuildMachine = Repository(cls=BuildMachine, collection=True)
Repository.PackagesDirectory = Repository(cls=PackagesDirectory, collection=True)

# ------------------------------------------------------------------------------


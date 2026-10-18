from __future__ import annotations

from .toolset import Toolset
from .toolset_application import ToolsetApplication


def toolset_has_applications(toolset: Toolset, applications) -> bool:
    return all(toolset.get_app_install(application) is not None for application in applications)


def sorted_toolsets(toolsets=None, applications=()) -> list[Toolset]:
    """Put the configured default first when it satisfies requirements, then newest first."""
    if toolsets is None:
        from .repository import Repository
        toolsets = Repository.Toolset.value
    from .repository import Repository
    settings_repository = getattr(Repository, "Settings", None)
    configured_id = getattr(getattr(settings_repository, "value", None), "default_toolset_id", None)
    return sorted(toolsets, key=lambda item: (
        bool(configured_id and str(getattr(item, "uuid", "")) == configured_id
             and toolset_has_applications(item, applications)),
        int(getattr(item, "metadata", {}).get("date_updated", 0) or 0),
        getattr(item, "name", "").lower()), reverse=True)


def default_toolset(applications=(ToolsetApplication.CATALYST,), toolsets=None) -> Toolset | None:
    compatible = [item for item in sorted_toolsets(toolsets, applications)
                  if toolset_has_applications(item, applications) and getattr(item, "busy_reason", None) is None]
    return compatible[0] if compatible else None


def build_toolset_requirements(project) -> list:
    applications = [ToolsetApplication.CATALYST]
    from .project_stage_portage_confdir import is_emulated_build
    architecture = project.get_architecture()
    if architecture is not None and is_emulated_build(architecture):
        applications.append(ToolsetApplication.QEMU)
    return applications

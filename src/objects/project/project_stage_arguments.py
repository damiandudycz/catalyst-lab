from __future__ import annotations
from typing import FrozenSet
from dataclasses import dataclass
from enum import Enum, auto
from .project_stage_automatic_option import StageAutomaticOption

@dataclass
class StageArguments:
    """Collects required and valid sets of arguments for given target."""
    required: FrozenSet[str]
    valid: FrozenSet[str]

@dataclass
class StageArgumentTargetDetails:
    """Gather details about argument of name for given target."""
    name: str
    required: bool
    details: StageArgumentDetails | None

    @property
    def display_name(self) -> str:
        return self.details.display_name if self.details else self.name

    @property
    def attribute_name(self) -> str:
        """Name of ProjectStage attribute that stores value of this argument."""
        return self.details.name if self.details else self.name

    @property
    def type(self) -> StageArgumentType:
        """Arguments not known to StageArgumentDetails are edited as single line text."""
        return self.details.type if self.details else StageArgumentType.raw_single_line

@dataclass
class StageArgumentOption:
    """Used to display options in lists and allowing to select them."""
    raw: str
    display: str
    subtitle: str | None
    value: Any
    argument: StageArgumentDetails
    unsupported: bool = False
    icon_name: str | None = None # Shown instead of icon of list, eg. target icon of parent stage.

class StageArgumentType(Enum):
    raw = auto() # Raw text data
    raw_single_line = auto() # Raw with only one line of text
    select = auto() # Select one option from predefined list
    multiselect = auto() # Select multiple options from predefined list
    boolean = auto() # yes / no
    string_list = auto() # List of values, edited one entry per line

class StageArgumentDetails(Enum):
    version_stamp = "version_stamp"
    target = "target"
    subarch = "subarch"
    rel_type = "rel_type"
    profile = "profile"
    interpreter = "interpreter"
    chost = "chost"
    cbuild = "cbuild"
    cxxflags = "cxxflags"
    cflags = "cflags"
    fcflags = "fcflags"
    fflags = "fflags"
    asflags = "asflags"
    ldflags = "ldflags"
    common_flags = "common_flags"
    hostuse = "hostuse"
    repos = "repos"
    binrepo_path = "binrepo_path"
    catalyst_use = "catalyst_use"
    compression_mode = "compression_mode"
    decompressor_search_order = "decompressor_search_order"
    install_mask = "install_mask"
    keep_repos = "keep_repos"
    kerncache_path = "kerncache_path"
    pkgcache_path = "pkgcache_path"
    portage_confdir = "portage_confdir"
    portage_prefix = "portage_prefix"
    snapshot_treeish = "snapshot_treeish"
    source_subpath = "source_subpath"
    update_seed = "update_seed"
    update_seed_command = "update_seed_command"
    rename_regexp = "rename_regexp"
    boot_kernel = 'boot/kernel'
    stage4_empty = 'stage4/empty'
    stage4_fsscript = 'stage4/fsscript'
    stage4_gk_mainargs = 'stage4/gk_mainargs'
    stage4_groups = 'stage4/groups'
    stage4_linuxrc = 'stage4/linuxrc'
    stage4_packages = 'stage4/packages'
    stage4_rcadd = 'stage4/rcadd'
    stage4_rcdel = 'stage4/rcdel'
    stage4_rm = 'stage4/rm'
    stage4_root_overlay = 'stage4/root_overlay'
    stage4_ssh_public_keys = 'stage4/ssh_public_keys'
    stage4_unmerge = 'stage4/unmerge'
    stage4_use = 'stage4/use'
    stage4_users = 'stage4/users'
    livecd_packages = 'livecd/packages'
    livecd_use = 'livecd/use'
    livecd_bootargs = 'livecd/bootargs'
    livecd_cdtar = 'livecd/cdtar'
    livecd_depclean = 'livecd/depclean'
    livecd_empty = 'livecd/empty'
    livecd_fsops = 'livecd/fsops'
    livecd_fsscript = 'livecd/fsscript'
    livecd_fstype = 'livecd/fstype'
    livecd_gk_mainargs = 'livecd/gk_mainargs'
    livecd_iso = 'livecd/iso'
    livecd_iso_extra_partition = 'livecd/iso_extra_partition'
    livecd_linuxrc = 'livecd/linuxrc'
    livecd_modblacklist = 'livecd/modblacklist'
    livecd_motd = 'livecd/motd'
    livecd_overlay = 'livecd/overlay'
    livecd_rcadd = 'livecd/rcadd'
    livecd_rcdel = 'livecd/rcdel'
    livecd_readme = 'livecd/readme'
    livecd_rm = 'livecd/rm'
    livecd_root_overlay = 'livecd/root_overlay'
    livecd_type = 'livecd/type'
    livecd_unmerge = 'livecd/unmerge'
    livecd_users = 'livecd/users'
    livecd_verify = 'livecd/verify'
    livecd_volid = 'livecd/volid'
    # Virtual properties:
    name = 'name'
    parent = 'parent'
    releng_template = 'releng_template'
    # ...

    @property
    def automatic_options(self) -> list[StageAutomaticOption]:
        """Automatic options (inheriting from parent, releng template...) allowed for this argument, in display order."""
        return _automatic_options_mapping.get(self, [])

    @property
    def allows_additions(self) -> bool:
        """Argument whose inherited value can be joined with own value (inherit options selected together with custom
        value, stored as options followed by own entries): lists are joined, texts are joined with spaces. Other
        arguments are inherited, or replaced by own values."""
        return self in _ARGUMENTS_WITH_ADDITIONS

    @property
    def default_options(self) -> list[StageAutomaticOption]:
        """Automatic options preferred as default value of new stage, in order of preference. First one available for
        the stage is used (parent stage selected, releng template selected and defining this argument...)."""
        return _default_options_mapping.get(self, [])

    @staticmethod
    def named(name: str) -> StageArgumentDetails | None:
        try:
            return StageArgumentDetails(name)
        except ValueError:
            return None

    @property
    def display_name(self) -> str:
        match self:
            case StageArgumentDetails.version_stamp: return "Version stamp"
            case StageArgumentDetails.profile: return "Profile"
            case StageArgumentDetails.repos: return "Repos"
            case StageArgumentDetails.target: return "Target"
            case StageArgumentDetails.asflags: return "ASFlags"
            case StageArgumentDetails.binrepo_path: return "Binrepo path"
            case StageArgumentDetails.catalyst_use: return "Catalyst USE"
            case StageArgumentDetails.cbuild: return "CBuild"
            case StageArgumentDetails.cflags: return "CFlags"
            case StageArgumentDetails.chost: return "CHost"
            case StageArgumentDetails.common_flags: return "Common flags"
            case StageArgumentDetails.compression_mode: return "Compression mode"
            case StageArgumentDetails.cxxflags: return "CXXFlags"
            case StageArgumentDetails.decompressor_search_order: return "Decompressor search order"
            case StageArgumentDetails.fcflags: return "FCFlags"
            case StageArgumentDetails.fflags: return "FFlags"
            case StageArgumentDetails.hostuse: return "Host USE"
            case StageArgumentDetails.install_mask: return "Install mask"
            case StageArgumentDetails.interpreter: return "Interpreter"
            case StageArgumentDetails.keep_repos: return "Keep repos"
            case StageArgumentDetails.kerncache_path: return "Kernel cache"
            case StageArgumentDetails.ldflags: return "LDFlags"
            case StageArgumentDetails.pkgcache_path: return "Package cache"
            case StageArgumentDetails.portage_confdir: return "Portage confdir"
            case StageArgumentDetails.portage_prefix: return "Portage prefix"
            case StageArgumentDetails.rel_type: return "Rel type"
            case StageArgumentDetails.snapshot_treeish: return "Snapshot treeish"
            case StageArgumentDetails.source_subpath: return "Source subpath"
            case StageArgumentDetails.subarch: return "Subarch"
            case StageArgumentDetails.update_seed: return "Update seed"
            case StageArgumentDetails.update_seed_command: return "Update seed command"
            case StageArgumentDetails.rename_regexp: return "Rename regexp"
            case StageArgumentDetails.boot_kernel: return "Boot / kernel"
            case StageArgumentDetails.stage4_empty: return "Empty"
            case StageArgumentDetails.stage4_fsscript: return "FS script"
            case StageArgumentDetails.stage4_gk_mainargs: return "GK mainargs"
            case StageArgumentDetails.stage4_groups: return "Groups"
            case StageArgumentDetails.stage4_linuxrc: return "LinuxRC"
            case StageArgumentDetails.stage4_packages: return "Packages"
            case StageArgumentDetails.stage4_rcadd: return "RCadd"
            case StageArgumentDetails.stage4_rcdel: return "RCdel"
            case StageArgumentDetails.stage4_rm: return "RM"
            case StageArgumentDetails.stage4_root_overlay: return "Root overlay"
            case StageArgumentDetails.stage4_ssh_public_keys: return "SSH public keys"
            case StageArgumentDetails.stage4_unmerge: return "Unmerge"
            case StageArgumentDetails.stage4_use: return "USE"
            case StageArgumentDetails.stage4_users: return "Users"
            case StageArgumentDetails.livecd_packages: return "Packages"
            case StageArgumentDetails.livecd_use: return "USE"
            case StageArgumentDetails.livecd_bootargs: return "BOOT args"
            case StageArgumentDetails.livecd_cdtar: return "CDTar"
            case StageArgumentDetails.livecd_depclean: return "Depclean"
            case StageArgumentDetails.livecd_empty: return "Empty"
            case StageArgumentDetails.livecd_fsops: return "FS ops"
            case StageArgumentDetails.livecd_fsscript: return "FS Script"
            case StageArgumentDetails.livecd_fstype: return "FS Type"
            case StageArgumentDetails.livecd_gk_mainargs: return "GK mainargs"
            case StageArgumentDetails.livecd_iso: return "ISO"
            case StageArgumentDetails.livecd_iso_extra_partition: return "ISO extra partition"
            case StageArgumentDetails.livecd_linuxrc: return "LinuxRC"
            case StageArgumentDetails.livecd_modblacklist: return "Modblacklist"
            case StageArgumentDetails.livecd_motd: return "Motd"
            case StageArgumentDetails.livecd_overlay: return "Overlay"
            case StageArgumentDetails.livecd_rcadd: return "RC add"
            case StageArgumentDetails.livecd_rcdel: return "RC del"
            case StageArgumentDetails.livecd_readme: return "Readme"
            case StageArgumentDetails.livecd_rm: return "RM"
            case StageArgumentDetails.livecd_root_overlay: return "Root overlay"
            case StageArgumentDetails.livecd_type: return "Type"
            case StageArgumentDetails.livecd_unmerge: return "Unmerge"
            case StageArgumentDetails.livecd_users: return "Users"
            case StageArgumentDetails.livecd_verify: return "Verify"
            case StageArgumentDetails.livecd_volid: return "Vol ID"
            # Virtual:
            case StageArgumentDetails.name: return "Name"
            case StageArgumentDetails.parent: return "Parent"
            case StageArgumentDetails.releng_template: return "Releng template"

    @property
    def type(self) -> StageArgumentType:
        match self:
            case StageArgumentDetails.profile: return StageArgumentType.select
            case StageArgumentDetails.target: return StageArgumentType.select
            case StageArgumentDetails.releng_template: return StageArgumentType.select
            case StageArgumentDetails.snapshot_treeish: return StageArgumentType.select
            case StageArgumentDetails.compression_mode: return StageArgumentType.select
            case StageArgumentDetails.parent: return StageArgumentType.select
            case StageArgumentDetails.subarch: return StageArgumentType.select
            case StageArgumentDetails.interpreter: return StageArgumentType.multiselect
            case StageArgumentDetails.repos: return StageArgumentType.multiselect
            case StageArgumentDetails.portage_confdir: return StageArgumentType.multiselect # Sources combined when building.
            case StageArgumentDetails.stage4_root_overlay | StageArgumentDetails.livecd_root_overlay | StageArgumentDetails.livecd_overlay: return StageArgumentType.multiselect # Like portage_confdir.
            case StageArgumentDetails.update_seed: return StageArgumentType.boolean
            case StageArgumentDetails.keep_repos: return StageArgumentType.boolean
            case (
                StageArgumentDetails.hostuse |
                StageArgumentDetails.catalyst_use |
                StageArgumentDetails.install_mask |
                StageArgumentDetails.decompressor_search_order |
                StageArgumentDetails.boot_kernel |
                StageArgumentDetails.stage4_empty |
                StageArgumentDetails.stage4_groups |
                StageArgumentDetails.stage4_packages |
                StageArgumentDetails.stage4_rcadd |
                StageArgumentDetails.stage4_rcdel |
                StageArgumentDetails.stage4_rm |
                StageArgumentDetails.stage4_unmerge |
                StageArgumentDetails.stage4_use |
                StageArgumentDetails.stage4_users |
                StageArgumentDetails.livecd_packages |
                StageArgumentDetails.livecd_use |
                StageArgumentDetails.livecd_empty |
                StageArgumentDetails.livecd_fsops |
                StageArgumentDetails.livecd_modblacklist |
                StageArgumentDetails.livecd_rcadd |
                StageArgumentDetails.livecd_rcdel |
                StageArgumentDetails.livecd_rm |
                StageArgumentDetails.livecd_unmerge |
                StageArgumentDetails.livecd_users
            ): return StageArgumentType.string_list
            case _: return StageArgumentType.raw_single_line


# Automatic options allowed for arguments. Arguments not listed here can only be set directly.
# Releng templates are spec files, which can define any argument, so inheriting from template is allowed wherever it
# makes sense, not only for arguments used by current releng specs.
_PARENT = StageAutomaticOption.INHERIT_FROM_PARENT
_RELENG = StageAutomaticOption.INHERIT_FROM_RELENG_TEMPLATE
_AUTO = StageAutomaticOption.GENERATE_AUTOMATICALLY
_automatic_options_mapping: dict[StageArgumentDetails, list[StageAutomaticOption]] = {
    # Basic:
    StageArgumentDetails.profile: [_PARENT, _RELENG],
    # Architecture:
    StageArgumentDetails.subarch: [_PARENT, _RELENG],
    StageArgumentDetails.chost: [_PARENT, _RELENG],
    StageArgumentDetails.cbuild: [_PARENT, _RELENG],
    StageArgumentDetails.interpreter: [_AUTO, _RELENG],
    StageArgumentDetails.asflags: [_PARENT, _RELENG],
    StageArgumentDetails.cflags: [_PARENT, _RELENG],
    StageArgumentDetails.common_flags: [_PARENT, _RELENG],
    StageArgumentDetails.cxxflags: [_PARENT, _RELENG],
    StageArgumentDetails.fcflags: [_PARENT, _RELENG],
    StageArgumentDetails.fflags: [_PARENT, _RELENG],
    StageArgumentDetails.ldflags: [_PARENT, _RELENG],
    # Release:
    StageArgumentDetails.rel_type: [_PARENT, _RELENG],
    StageArgumentDetails.version_stamp: [_PARENT, _RELENG],
    StageArgumentDetails.compression_mode: [_AUTO, _PARENT, _RELENG],
    # Packages:
    StageArgumentDetails.repos: [_PARENT, _RELENG],
    StageArgumentDetails.keep_repos: [_PARENT, _RELENG],
    StageArgumentDetails.binrepo_path: [_PARENT, _RELENG],
    # Configuration:
    StageArgumentDetails.source_subpath: [_AUTO], # Generated from parent stage output, hidden in UI.
    StageArgumentDetails.portage_prefix: [_PARENT, _RELENG],
    StageArgumentDetails.update_seed: [_PARENT, _RELENG],
    StageArgumentDetails.update_seed_command: [_PARENT, _RELENG],
    StageArgumentDetails.rename_regexp: [_RELENG],
    StageArgumentDetails.install_mask: [_PARENT, _RELENG],
    StageArgumentDetails.catalyst_use: [_PARENT, _RELENG],
    StageArgumentDetails.decompressor_search_order: [_PARENT, _RELENG],
    StageArgumentDetails.hostuse: [_PARENT, _RELENG],
    # Caches are automatic (shared by stages with the same rel_type), selected folder or disabled
    # (project_stage_cache). They are not inherited, sharing cache between stages built with different flags could mix
    # incompatible binaries.
    StageArgumentDetails.pkgcache_path: [_AUTO],
    StageArgumentDetails.kerncache_path: [_AUTO],
    # Packages of a stage4 may be inherited from its parent and joined with extra packages.
    StageArgumentDetails.stage4_packages: [_PARENT, _RELENG],
    # Target specific (stage4/*, livecd/*, boot/kernel) are defined by releng templates.
    **{
        argument: [_RELENG]
        for argument in StageArgumentDetails
        if argument.value.split("/")[0] in ("stage4", "livecd", "boot")
        # Root and ISO overlays are combined from their own sources, like portage_confdir.
        and argument not in (StageArgumentDetails.stage4_root_overlay, StageArgumentDetails.livecd_root_overlay,
                             StageArgumentDetails.livecd_overlay, StageArgumentDetails.stage4_packages)
    },
}

# Defaults for new stages, in order of preference. First one available for stage is used. Releng template is available
# only when selected template defines the argument, so it's safe to list it for every argument it could define.
# Values that must stay consistent along the stages chain (seed compatibility) prefer parent. Values describing given
# build prefer releng template, which is chosen for it.
_default_options_mapping: dict[StageArgumentDetails, list[StageAutomaticOption]] = {
    # Basic:
    StageArgumentDetails.profile: [_RELENG, _PARENT], # Template can use more specific profile than seed (eg. desktop).
    # Architecture:
    StageArgumentDetails.subarch: [_PARENT, _RELENG], # Toolchain needs to match the seed.
    StageArgumentDetails.chost: [_PARENT, _RELENG],
    StageArgumentDetails.cbuild: [_PARENT, _RELENG],
    StageArgumentDetails.interpreter: [_RELENG, _AUTO],
    StageArgumentDetails.asflags: [_RELENG, _PARENT], # Flags set by template are meant for this build.
    StageArgumentDetails.cflags: [_RELENG, _PARENT],
    StageArgumentDetails.common_flags: [_RELENG, _PARENT],
    StageArgumentDetails.cxxflags: [_RELENG, _PARENT],
    StageArgumentDetails.fcflags: [_RELENG, _PARENT],
    StageArgumentDetails.fflags: [_RELENG, _PARENT],
    StageArgumentDetails.ldflags: [_RELENG, _PARENT],
    # Release:
    StageArgumentDetails.rel_type: [_PARENT, _RELENG], # Whole chain is built in the same rel_type directory.
    StageArgumentDetails.version_stamp: [_RELENG, _PARENT], # Variant of seed (eg. openrc) when template doesn't set it.
    StageArgumentDetails.compression_mode: [_RELENG, _AUTO],
    # Packages:
    StageArgumentDetails.repos: [_PARENT, _RELENG],
    StageArgumentDetails.keep_repos: [_PARENT, _RELENG],
    StageArgumentDetails.binrepo_path: [_RELENG, _PARENT],
    # Configuration:
    StageArgumentDetails.source_subpath: [_AUTO],
    # portage_confdir is combined from its own sources (project_stage_portage_confdir), defaults are set there.
    StageArgumentDetails.portage_prefix: [_RELENG, _PARENT],
    StageArgumentDetails.update_seed: [_RELENG, _PARENT],
    StageArgumentDetails.update_seed_command: [_RELENG, _PARENT],
    StageArgumentDetails.rename_regexp: [_RELENG],
    StageArgumentDetails.install_mask: [_RELENG, _PARENT], # Resulting system should be consistent along the chain.
    StageArgumentDetails.catalyst_use: [_RELENG, _PARENT],
    StageArgumentDetails.decompressor_search_order: [_RELENG, _PARENT],
    StageArgumentDetails.hostuse: [_RELENG], # Usually set only for stage that needs it (stage1).
    StageArgumentDetails.boot_kernel: [_RELENG],
    StageArgumentDetails.pkgcache_path: [_AUTO],
    StageArgumentDetails.kerncache_path: [_AUTO],
    # Target specific:
    **{
        argument: [_RELENG]
        for argument in StageArgumentDetails
        if argument.value.split("/")[0] in ("stage4", "livecd", "boot")
        # Root and ISO overlays are combined from their own sources, like portage_confdir.
        and argument not in (StageArgumentDetails.stage4_root_overlay, StageArgumentDetails.livecd_root_overlay,
                             StageArgumentDetails.livecd_overlay)
    },
}

# ------------------------------------------------------------------------------
# Basic and advanced settings of stages: stage details show basic settings, or all of them in advanced mode.

class StageArgumentLevel(Enum):
    BASIC = "basic"       # Shown in basic and advanced mode.
    ADVANCED = "advanced" # Shown only in advanced mode.

_BASIC = StageArgumentLevel.BASIC
_ADVANCED = StageArgumentLevel.ADVANCED

# Level of every argument known to the app. Arguments of catalyst which app doesn't describe are advanced.
STAGE_ARGUMENT_LEVELS: dict[StageArgumentDetails, StageArgumentLevel] = {
    # Stage and its source:
    StageArgumentDetails.parent: _BASIC,
    StageArgumentDetails.target: _BASIC,
    StageArgumentDetails.releng_template: _BASIC,
    StageArgumentDetails.profile: _BASIC,
    # Architecture and compiler flags (common flags apply to all languages):
    StageArgumentDetails.subarch: _BASIC,
    StageArgumentDetails.common_flags: _BASIC,
    StageArgumentDetails.chost: _ADVANCED,
    StageArgumentDetails.cbuild: _ADVANCED,
    StageArgumentDetails.cflags: _ADVANCED,
    StageArgumentDetails.cxxflags: _ADVANCED,
    StageArgumentDetails.fcflags: _ADVANCED,
    StageArgumentDetails.fflags: _ADVANCED,
    StageArgumentDetails.asflags: _ADVANCED,
    StageArgumentDetails.ldflags: _ADVANCED,
    StageArgumentDetails.interpreter: _ADVANCED,
    # Release:
    StageArgumentDetails.rel_type: _BASIC,
    StageArgumentDetails.version_stamp: _BASIC,
    StageArgumentDetails.compression_mode: _ADVANCED,
    StageArgumentDetails.decompressor_search_order: _ADVANCED,
    # Packages and Portage:
    StageArgumentDetails.repos: _BASIC,
    StageArgumentDetails.portage_confdir: _ADVANCED,
    StageArgumentDetails.keep_repos: _ADVANCED,
    StageArgumentDetails.binrepo_path: _ADVANCED,
    StageArgumentDetails.pkgcache_path: _ADVANCED,
    StageArgumentDetails.kerncache_path: _ADVANCED,
    StageArgumentDetails.catalyst_use: _ADVANCED,
    StageArgumentDetails.hostuse: _ADVANCED,
    StageArgumentDetails.install_mask: _ADVANCED,
    StageArgumentDetails.portage_prefix: _ADVANCED,
    StageArgumentDetails.update_seed: _ADVANCED,
    StageArgumentDetails.update_seed_command: _ADVANCED,
    StageArgumentDetails.rename_regexp: _ADVANCED,
    # Set by app, not shown (listed so the map covers all arguments):
    StageArgumentDetails.name: _BASIC,
    StageArgumentDetails.snapshot_treeish: _ADVANCED,
    StageArgumentDetails.source_subpath: _ADVANCED,
    # Kernels:
    StageArgumentDetails.boot_kernel: _BASIC,
    # Stage4: what system contains and how it starts.
    StageArgumentDetails.stage4_packages: _BASIC,
    StageArgumentDetails.stage4_use: _BASIC,
    StageArgumentDetails.stage4_rcadd: _BASIC,
    StageArgumentDetails.stage4_users: _BASIC,
    StageArgumentDetails.stage4_root_overlay: _ADVANCED,
    StageArgumentDetails.stage4_fsscript: _ADVANCED,
    StageArgumentDetails.stage4_ssh_public_keys: _ADVANCED,
    StageArgumentDetails.stage4_rcdel: _ADVANCED,
    StageArgumentDetails.stage4_groups: _ADVANCED,
    StageArgumentDetails.stage4_unmerge: _ADVANCED,
    StageArgumentDetails.stage4_empty: _ADVANCED,
    StageArgumentDetails.stage4_rm: _ADVANCED,
    StageArgumentDetails.stage4_gk_mainargs: _ADVANCED,
    StageArgumentDetails.stage4_linuxrc: _ADVANCED,
    # LiveCD: contents and ISO.
    StageArgumentDetails.livecd_packages: _BASIC,
    StageArgumentDetails.livecd_use: _BASIC,
    StageArgumentDetails.livecd_iso: _BASIC,
    StageArgumentDetails.livecd_volid: _BASIC,
    StageArgumentDetails.livecd_type: _BASIC,
    StageArgumentDetails.livecd_fstype: _BASIC,
    StageArgumentDetails.livecd_bootargs: _BASIC,
    StageArgumentDetails.livecd_rcadd: _BASIC,
    StageArgumentDetails.livecd_root_overlay: _ADVANCED,
    StageArgumentDetails.livecd_overlay: _ADVANCED,
    StageArgumentDetails.livecd_rcdel: _ADVANCED,
    StageArgumentDetails.livecd_users: _ADVANCED,
    StageArgumentDetails.livecd_fsscript: _ADVANCED,
    StageArgumentDetails.livecd_unmerge: _ADVANCED,
    StageArgumentDetails.livecd_empty: _ADVANCED,
    StageArgumentDetails.livecd_rm: _ADVANCED,
    StageArgumentDetails.livecd_depclean: _ADVANCED,
    StageArgumentDetails.livecd_cdtar: _ADVANCED,
    StageArgumentDetails.livecd_fsops: _ADVANCED,
    StageArgumentDetails.livecd_gk_mainargs: _ADVANCED,
    StageArgumentDetails.livecd_linuxrc: _ADVANCED,
    StageArgumentDetails.livecd_modblacklist: _ADVANCED,
    StageArgumentDetails.livecd_motd: _ADVANCED,
    StageArgumentDetails.livecd_readme: _ADVANCED,
    StageArgumentDetails.livecd_verify: _ADVANCED,
}

def stage_argument_level(details: StageArgumentDetails | None) -> StageArgumentLevel:
    """Arguments not known to app (read from catalyst) are advanced."""
    return STAGE_ARGUMENT_LEVELS.get(details, StageArgumentLevel.ADVANCED) if details else StageArgumentLevel.ADVANCED

# Arguments where own value is joined with inherited one: lists (packages, USE flags, services, overlays, kernel
# names...) and texts made of options (compiler flags, arguments of genkernel and bootloader). Ordered lists
# (decompressor search order, filesystem operations), kernel settings, and single values (paths, names) are not joined.
_ARGUMENTS_WITH_ADDITIONS = {
    StageArgumentDetails.repos,
    StageArgumentDetails.stage4_users, StageArgumentDetails.stage4_groups, StageArgumentDetails.livecd_users,
    StageArgumentDetails.common_flags, StageArgumentDetails.cflags, StageArgumentDetails.cxxflags,
    StageArgumentDetails.fcflags, StageArgumentDetails.fflags, StageArgumentDetails.asflags, StageArgumentDetails.ldflags,
    StageArgumentDetails.stage4_gk_mainargs, StageArgumentDetails.livecd_gk_mainargs, StageArgumentDetails.livecd_bootargs,
    StageArgumentDetails.boot_kernel,
    StageArgumentDetails.stage4_packages, StageArgumentDetails.livecd_packages,
    StageArgumentDetails.stage4_use, StageArgumentDetails.livecd_use,
    StageArgumentDetails.stage4_rcadd, StageArgumentDetails.livecd_rcadd,
    StageArgumentDetails.stage4_rcdel, StageArgumentDetails.livecd_rcdel,
    StageArgumentDetails.stage4_unmerge, StageArgumentDetails.livecd_unmerge,
    StageArgumentDetails.stage4_empty, StageArgumentDetails.livecd_empty,
    StageArgumentDetails.stage4_rm, StageArgumentDetails.livecd_rm,
    StageArgumentDetails.livecd_modblacklist,
    StageArgumentDetails.catalyst_use, StageArgumentDetails.hostuse, StageArgumentDetails.install_mask,
}

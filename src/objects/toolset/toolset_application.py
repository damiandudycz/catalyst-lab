from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Tuple, ClassVar
from pathlib import Path
from collections import namedtuple
from .architecture import Emulation
from .root_helper_client import ServerCall
import uuid

# ------------------------------------------------------------------------------
# Toolset applications.

@dataclass(frozen=True)
class ToolsetApplication:
    """Additional tools installed in toolsets, like Catalyst, Qemu."""
    ALL: ClassVar[list[ToolsetApplication]] = []
    name: str
    description: str
    package: str
    is_recommended: bool = False
    is_highly_recommended: bool = False
    versions: Tuple[Version, ...] = field(default_factory=tuple)
    dependencies: Tuple[ToolsetApplication, ...] = field(default_factory=tuple)
    auto_select: bool = False # Automatically select / deselect for apps that depends on this one.
    toolset_additional_analysis: Callable[[ToolsetApplication,Toolset,ServerCall|None],None] | None = None # Additional analysis for toolset
    def __post_init__(self):
        # Automatically add new instances to ToolsetApplication.ALL
        ToolsetApplication.ALL.append(self)

ToolsetApplicationSelection = namedtuple("ToolsetApplicationSelection", ["app", "version", "selected", "patches"])
ToolsetApplicationInstall = namedtuple("ToolsetApplicationInstall", ["version", "variant", "patches"])

@dataclass(frozen=True)
class PortageConfig:
     # eq: { "packages.use": ["Entry1", "Entry2"], "package.accept_keywords": ["Entry1", "Entry2"] }
    directory: str
    entries: Tuple[str, ...] = field(default_factory=tuple)
@dataclass(frozen=True)
class ToolsetApplicationVersion:
    name: str
    id: uuid.UUID
    config: PortageConfig = field(default_factory=PortageConfig)

ToolsetApplication.CATALYST = ToolsetApplication(
    name="Catalyst", description="Required to build Gentoo stages",
    package="dev-util/catalyst",
    is_recommended=True, is_highly_recommended=True,
    versions=(
        ToolsetApplicationVersion(
            name="Stable",
            id=uuid.UUID("068688a1-1b31-43ea-b8ef-70c2857ea903"),
            config=(
                PortageConfig(directory="package.accept_keywords", entries=("dev-util/catalyst",)),
                PortageConfig(
                    directory="package.use",
                    entries=(
                        ">=sys-apps/util-linux-2.40.4 python",
                        ">=sys-boot/grub-2.12-r6 grub_platforms_efi-64",
                        ">=sys-boot/grub-2.12-r6 grub_platforms_efi-32",
                    )
                ),
            )
        ),
        ToolsetApplicationVersion(
            name="Experimental",
            id=uuid.UUID("24e05851-c210-4f80-9bec-fb306ce32ba1"),
            config=(
                PortageConfig(directory="package.accept_keywords", entries=("dev-util/catalyst **",)),
                PortageConfig(
                    directory="package.use",
                    entries=(
                        ">=sys-apps/util-linux-2.40.4 python",
                        ">=sys-boot/grub-2.12-r6 grub_platforms_efi-64",
                        ">=sys-boot/grub-2.12-r6 grub_platforms_efi-32",
                    )
                ),
            )
        ),
    )
)
ToolsetApplication.LINUX_HEADERS = ToolsetApplication(
    name="Linux headers", description="Needed for qemu/cmake",
    package="sys-kernel/linux-headers",
    auto_select=True,
    versions=(
            ToolsetApplicationVersion(
                name="Stable",
                id=uuid.UUID("e5ef8be1-e11b-42c4-a824-fde690b42f46"),
                config=None
            ),
        ),
)
def toolset_additional_analysis_qemu(app: ToolsetApplication, toolset: 'Toolset', metadata: dict[str, Any]):
    binaries = {name for name, kind in toolset.list_toolset_directory("bin") or [] if kind == "file"}
    found_qemu_binaries = [qemu_binary for qemu_binary in Emulation.get_all_qemu_systems() if qemu_binary in binaries]
    metadata.setdefault(app.package, {})["interpreters"] = found_qemu_binaries

ToolsetApplication.QEMU = ToolsetApplication(
    name="Qemu", description="Allows building stages for different architectures",
    package="app-emulation/qemu",
    is_recommended=True,
    versions=(
        ToolsetApplicationVersion(
            name="Stable",
            id=uuid.UUID("bed67bf2-1ca7-4b06-9ca3-d4b1f2533102"),
            config=(
                PortageConfig(
                    directory="package.use",
                    entries=(
                        "app-emulation/qemu static-user",
                        "dev-libs/glib static-libs",
                        "sys-libs/zlib static-libs",
                        "sys-apps/attr static-libs",
                        "dev-libs/libpcre2 static-libs",
                        "sys-libs/libcap static-libs",
                        "*/* QEMU_SOFTMMU_TARGETS: aarch64 aarch64_be alpha arm armeb hexagon hppa i386 loongarch64 m68k microblaze microblazeel mips mips64 mips64el mipsel mipsn32 mipsn32el or1k ppc ppc64 ppc64le riscv32 riscv64 s390x sh4 sh4eb sparc sparc32plus sparc64 x86_64 xtensa xtensaeb",
                        "*/* QEMU_USER_TARGETS: aarch64 aarch64_be alpha arm armeb hexagon hppa i386 loongarch64 m68k microblaze microblazeel mips mips64 mips64el mipsel mipsn32 mipsn32el or1k ppc ppc64 ppc64le riscv32 riscv64 s390x sh4 sh4eb sparc sparc32plus sparc64 x86_64 xtensa xtensaeb",
                    )
                ),
            )
        ),
        ToolsetApplicationVersion(
            name="Experimental",
            id=uuid.UUID("4f73b683-baeb-4e81-ba50-f4e6c019a60a"),
            config=(
                PortageConfig(directory="package.accept_keywords", entries=("app-emulation/qemu **",)),
                PortageConfig(
                    directory="package.use",
                    entries=(
                        "app-emulation/qemu static-user",
                        "dev-libs/glib static-libs",
                        "sys-libs/zlib static-libs",
                        "sys-apps/attr static-libs",
                        "dev-libs/libpcre2 static-libs",
                        "sys-libs/libcap static-libs",
                        "*/* QEMU_SOFTMMU_TARGETS: aarch64 aarch64_be alpha arm armeb hexagon hppa i386 loongarch64 m68k microblaze microblazeel mips mips64 mips64el mipsel mipsn32 mipsn32el or1k ppc ppc64 ppc64le riscv32 riscv64 s390x sh4 sh4eb sparc sparc32plus sparc64 x86_64 xtensa xtensaeb",
                        "*/* QEMU_USER_TARGETS: aarch64 aarch64_be alpha arm armeb hexagon hppa i386 loongarch64 m68k microblaze microblazeel mips mips64 mips64el mipsel mipsn32 mipsn32el or1k ppc ppc64 ppc64le riscv32 riscv64 s390x sh4 sh4eb sparc sparc32plus sparc64 x86_64 xtensa xtensaeb",
                    )
                ),
            )
        ),
    ),
    dependencies=(ToolsetApplication.LINUX_HEADERS,),
    toolset_additional_analysis=toolset_additional_analysis_qemu
)


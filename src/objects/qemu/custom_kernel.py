"""Validation and Portage commands for kernels used by disposable test VMs."""
from __future__ import annotations
import os
import re
import shlex
import shutil

_PACKAGE_ATOM = re.compile(r"^[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+(?:::[A-Za-z0-9+_.-]+)?$")


def valid_kernel_package(value: str) -> bool:
    """Accept a category/package atom, optionally qualified by an enabled repository."""
    return isinstance(value, str) and bool(_PACKAGE_ATOM.fullmatch(value.strip()))


def copy_kernel_outputs(source: str, destination: str) -> None:
    """Copy boot files and modules, preserving kernel-package symlinks.

    Kernel module trees commonly contain dangling ``build`` and ``source`` links
    when exported from a disposable toolset. Following those links makes the
    cache-to-temporary copy fail, even though the module files are usable.
    """
    os.makedirs(destination, exist_ok=True)
    shutil.copy2(os.path.join(source, "custom-kernel"), os.path.join(destination, "custom-kernel"))
    shutil.copy2(os.path.join(source, "custom-initramfs"), os.path.join(destination, "custom-initramfs"))
    shutil.copytree(os.path.join(source, "custom-modules"), os.path.join(destination, "custom-modules"),
                    dirs_exist_ok=True, symlinks=True)


def is_arm64_linux_image(path: str) -> bool:
    """Return whether path starts with the ARM64 Linux Image boot header.

    Apple Virtualization's direct Linux loader needs the raw arm64 Image. EFI
    executables such as Gentoo's ``vmlinuz.efi`` are PE files and are rejected.
    """
    try:
        with open(path, "rb") as kernel:
            kernel.seek(0x38)
            return kernel.read(4) == b"ARM\x64"
    except OSError:
        return False


def kernel_package_build_command(package: str, use_binary_packages: bool = False,
                                 require_arm64_image: bool = False) -> str:
    """Emerge a kernel in the disposable toolset and export boot files and modules."""
    if not valid_kernel_package(package):
        raise ValueError("Enter a Portage package atom such as sys-kernel/gentoo-kernel")
    package = shlex.quote(package.strip())
    usepkg = " --usepkg" if use_binary_packages else ""
    kernel_selection = '''kernel=$(find /boot -maxdepth 1 -type f -name 'Image*' -print | sort -V | tail -n 1)
if [ -z "$kernel" ]; then
    kernel=$(find /usr/src -type f -path '*/arch/arm64/boot/Image' -print 2>/dev/null | sort -V | tail -n 1)
fi
[ -n "$kernel" ] || { echo "Apple Virtualization requires a raw ARM64 Image; this package only installed an EFI/PE kernel. Choose a source kernel package such as sys-kernel/gentoo-kernel instead of a -bin package."; exit 1; }
'''
    if not require_arm64_image:
        kernel_selection = '''kernel=$(find /boot -maxdepth 1 -type f \\
    \\( -name 'vmlinuz*' -o -name 'vmlinux*' -o -name 'Image*' \\) -print | sort -V | tail -n 1)
'''
    return f'''set -e
command -v emerge >/dev/null || {{ echo "Portage is not installed in the project toolset"; exit 1; }}
if [ -d /etc/portage/package.use ]; then
    printf '%s\\n' 'sys-kernel/installkernel dracut' \\
        > /etc/portage/package.use/99-catalystlab-test-vm-kernel
else
    printf '%s\\n' 'sys-kernel/installkernel dracut' >> /etc/portage/package.use
fi
echo "Emerging custom kernel package {package} in the temporary toolset"
emerge --ask=n --oneshot --verbose{usepkg} {package}
{kernel_selection}
initramfs=$(find /boot -maxdepth 1 -type f \\
    \\( -name 'initramfs*' -o -name 'initrd*' \\) -print | sort -V | tail -n 1)
[ -n "$kernel" ] || {{ echo "Kernel package did not install a supported image in /boot"; exit 1; }}
release=$(find /lib/modules -mindepth 1 -maxdepth 1 -type d -print | sort -V | tail -n 1)
[ -n "$release" ] || {{ echo "Kernel package did not install modules"; exit 1; }}
if [ -z "$initramfs" ]; then
    echo "Kernel package did not install an initramfs; generating one with dracut"
    command -v dracut >/dev/null || emerge --ask=n --oneshot --verbose{usepkg} sys-kernel/dracut
    dracut --force --no-hostonly --kver "$(basename "$release")" \\
        /mnt/catalystlab-kernel-output/custom-initramfs
else
    cp "$initramfs" /mnt/catalystlab-kernel-output/custom-initramfs
fi
cp "$kernel" /mnt/catalystlab-kernel-output/custom-kernel
rm -rf /mnt/catalystlab-kernel-output/custom-modules
mkdir -p /mnt/catalystlab-kernel-output/custom-modules
cp -a "$release" /mnt/catalystlab-kernel-output/custom-modules/
chmod a+r /mnt/catalystlab-kernel-output/custom-kernel /mnt/catalystlab-kernel-output/custom-initramfs
echo "Prepared $(basename "$kernel") with initramfs and modules for $release"
'''

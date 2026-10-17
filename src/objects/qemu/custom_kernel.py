"""Validation and Portage commands for kernels used by disposable test VMs."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
import re
import shlex
import shutil

_PACKAGE_ATOM = re.compile(r"^[A-Za-z0-9+_.-]+/[A-Za-z0-9+_.-]+(?:::[A-Za-z0-9+_.-]+)?$")
KERNEL_OUTPUTS_MANIFEST = ".kernel-output"
KERNEL_BUILD_METADATA = ".kernel-build.json"


def write_kernel_build_metadata(directory: str, metadata: dict) -> str:
    """Atomically save provenance for the reusable kernel outputs."""
    path = os.path.join(directory, KERNEL_BUILD_METADATA)
    temporary = path + ".tmp"
    document = {"schema_version": 1,
                "built_at": metadata.get("built_at", datetime.now(timezone.utc).isoformat()),
                **metadata}
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(document, file, indent=2, sort_keys=True)
        file.write("\n")
    os.replace(temporary, path)
    return path


def valid_kernel_package(value: str) -> bool:
    """Accept a category/package atom, optionally qualified by an enabled repository."""
    return isinstance(value, str) and bool(_PACKAGE_ATOM.fullmatch(value.strip()))


def copy_kernel_outputs(source: str, destination: str) -> None:
    """Copy boot files and the module archive into a VM workspace.

    The Library can live on a case-insensitive filesystem, while Linux module
    trees contain filenames that differ only by case. Keep the tree archived
    until it is extracted inside the VM's Linux filesystem.
    """
    os.makedirs(destination, exist_ok=True)
    shutil.copy2(os.path.join(source, "custom-kernel"), os.path.join(destination, "custom-kernel"))
    shutil.copy2(os.path.join(source, "custom-initramfs"), os.path.join(destination, "custom-initramfs"))
    shutil.copy2(os.path.join(source, "custom-modules.tar"), os.path.join(destination, "custom-modules.tar"))


def portage_config_setup_command() -> str:
    """Merge safe toolset and stage config into the writable run config mount."""
    return ("mkdir -p /etc/portage; "
            "if [ -d /var/tmp/catalystlab-base-portage ]; then "
            "find /var/tmp/catalystlab-base-portage -mindepth 1 -maxdepth 1 ! -name gnupg "
            "-exec cp -R {} /etc/portage/ \\;; fi; "
            "cp -R /var/tmp/catalystlab-stage-portage/. /etc/portage/; ")


def kernel_package_build_command(package: str, use_binary_packages: bool = False) -> str:
    """Emerge a kernel in the disposable toolset and export boot files and modules."""
    if not valid_kernel_package(package):
        raise ValueError("Enter a Portage package atom such as sys-kernel/gentoo-kernel")
    package = shlex.quote(package.strip())
    usepkg = " --usepkg" if use_binary_packages else ""
    kernel_selection = '''release=$(find /lib/modules -mindepth 1 -maxdepth 1 -type d -print | sort -V | tail -n 1)
[ -n "$release" ] || { echo "Kernel package did not install modules"; exit 1; }
kernel=""
build_kernel="$release/build/arch/arm64/boot/Image"
if [ -f "$build_kernel" ]; then
    kernel="$build_kernel"
    echo "Using raw ARM64 Linux Image from the matching kernel build tree: $kernel"
fi
if [ -z "$kernel" ]; then
    kernel=$(find /boot -maxdepth 1 -type f \\
    \\( -name 'vmlinuz*' -o -name 'vmlinux*' -o -name 'Image*' \\) -print | sort -V | tail -n 1)
    [ -z "$kernel" ] || echo "No raw ARM64 Image found in the matching build tree; using /boot kernel: $kernel"
fi
'''
    return f'''set -e
rm -f /var/tmp/catalystlab-kernel-output/{KERNEL_OUTPUTS_MANIFEST}
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
if [ -z "$initramfs" ]; then
    echo "Kernel package did not install an initramfs; generating one with dracut"
    command -v dracut >/dev/null || emerge --ask=n --oneshot --verbose{usepkg} sys-kernel/dracut
    dracut --force --no-hostonly --kver "$(basename "$release")" \\
        /var/tmp/catalystlab-kernel-output/custom-initramfs
else
    cp "$initramfs" /var/tmp/catalystlab-kernel-output/custom-initramfs
fi
python3 - "$kernel" /var/tmp/catalystlab-kernel-output/custom-kernel <<'PY'
import struct
import subprocess
import sys
import zlib

source, destination = sys.argv[1:]
data = open(source, "rb").read()

def is_arm64_image(image):
    return len(image) >= 64 and image[56:60] == b"ARM\\x64"

if is_arm64_image(data):
    image = data
else:
    print("Extracting raw ARM64 Image from the EFI_ZBOOT kernel package")
    # EFI_ZBOOT places the compressed Image in a PE section (usually .rodata).
    # Read section bounds so bytes in the EFI stub are never mistaken for payload.
    sections = []
    try:
        pe = struct.unpack_from("<I", data, 0x3c)[0]
        if data[pe:pe + 4] == b"PE\\0\\0":
            count = struct.unpack_from("<H", data, pe + 6)[0]
            optional_size = struct.unpack_from("<H", data, pe + 20)[0]
            table = pe + 24 + optional_size
            for index in range(count):
                offset = table + index * 40
                size, start = struct.unpack_from("<II", data, offset + 16)
                if size and start + size <= len(data):
                    sections.append(data[start:start + size])
    except (IndexError, struct.error):
        pass
    if not sections:
        sections = [data]

    image = None
    for section in sections:
        for magic, kind in ((b"\\x1f\\x8b\\x08", "gzip"),
                            (b"\\x28\\xb5\\x2f\\xfd", "zstd")):
            offset = 0
            while True:
                offset = section.find(magic, offset)
                if offset < 0:
                    break
                payload = section[offset:]
                try:
                    if kind == "gzip":
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        candidate = decoder.decompress(payload)
                        if not decoder.eof:
                            raise ValueError("incomplete gzip stream")
                    else:
                        result = subprocess.run(["zstd", "-d", "-q", "-c"], input=payload,
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        candidate = result.stdout
                    if is_arm64_image(candidate):
                        image = candidate
                        break
                except (OSError, ValueError, zlib.error):
                    pass
                offset += len(magic)
            if image is not None:
                break
        if image is not None:
            break
    if image is None:
        raise SystemExit("Kernel image is neither a raw ARM64 Image nor a supported EFI_ZBOOT image")

# The ARM64 header's image_size excludes any trailing package padding.
size = struct.unpack_from("<Q", image, 16)[0]
if 64 <= size <= len(image):
    image = image[:size]
with open(destination, "wb") as output:
    output.write(image)
PY
rm -f /var/tmp/catalystlab-kernel-output/custom-modules.tar
tar -cf /var/tmp/catalystlab-kernel-output/custom-modules.tar -C /lib/modules "$(basename "$release")"
chmod a+r /var/tmp/catalystlab-kernel-output/custom-kernel /var/tmp/catalystlab-kernel-output/custom-initramfs
printf '%s\\n' {package} "$(basename "$release")" > /var/tmp/catalystlab-kernel-output/{KERNEL_OUTPUTS_MANIFEST}
echo "Prepared $(basename "$kernel") with initramfs and modules for $release"
'''

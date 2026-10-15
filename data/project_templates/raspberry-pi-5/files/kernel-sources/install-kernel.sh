#!/bin/bash
# Installs Raspberry Pi 5 kernel built by catalyst (kernel rpi of stage, genkernel from sys-kernel/raspberrypi-sources)
# the way firmware boots it, like sys-kernel/raspberrypi-image: kernel_2712.img, device trees and overlays in /boot.
# Genkernel installs kernel with its own name and doesn't build device trees. Run by catalyst as fsscript of stage4,
# after kernel is built.
set -euo pipefail

SOURCES=$(readlink -f /usr/src/linux)
[ -f "$SOURCES/.config" ] || { echo "Built kernel sources not found in /usr/src/linux"; exit 1; }
echo "Installing kernel built from $SOURCES"

# Uncompressed image from build tree, otherwise the one installed by genkernel.
if [ -f "$SOURCES/arch/arm64/boot/Image" ]; then
    cp "$SOURCES/arch/arm64/boot/Image" /boot/kernel_2712.img
else
    KERNEL=$(ls -t /boot/vmlinuz-* /boot/kernel-* 2> /dev/null | head -n 1)
    [ -n "$KERNEL" ] || { echo "Kernel built by genkernel not found in /boot"; exit 1; }
    cp "$KERNEL" /boot/kernel_2712.img
fi

make -C "$SOURCES" -j"$(nproc)" dtbs
mkdir -p /boot/overlays
cp "$SOURCES"/arch/arm64/boot/dts/broadcom/bcm2712*.dtb /boot/
cp "$SOURCES"/arch/arm64/boot/dts/overlays/*.dtb* /boot/overlays/
[ ! -f "$SOURCES/arch/arm/boot/dts/overlays/README" ] || cp "$SOURCES/arch/arm/boot/dts/overlays/README" /boot/overlays/

# Catalyst removes kernel sources from world after building them, they stay in it so system gets their updates.
emerge --noreplace --quiet sys-kernel/raspberrypi-sources

# Script building kernel again on Raspberry Pi (root overlay of stage) can be run directly.
[ ! -f /usr/local/sbin/build-raspberrypi-kernel ] || chmod 755 /usr/local/sbin/build-raspberrypi-kernel
echo "Installed kernel $(make -s -C "$SOURCES" kernelrelease) as /boot/kernel_2712.img"

#!/bin/bash
# Compiles Raspberry Pi 5 kernel from sys-kernel/raspberrypi-sources and installs it like sys-kernel/raspberrypi-image:
# kernel_2712.img (loaded by firmware), device trees and overlays in /boot, modules in /lib/modules. Steps of Raspberry
# Pi documentation (https://www.raspberrypi.com/documentation/computers/linux_kernel.html#building). Installed as
# /usr/local/sbin/build-raspberrypi-kernel, run it on Raspberry Pi to build kernel from new sources (after update of
# raspberrypi-sources).
set -euo pipefail

SOURCES=$(ls -d /usr/src/linux-*raspberrypi* 2> /dev/null | sort -V | tail -n 1)
[ -n "$SOURCES" ] || { echo "Sources of sys-kernel/raspberrypi-sources not found in /usr/src"; exit 1; }
BUILD=/var/tmp/raspberrypi-kernel-build
JOBS=$(nproc)
echo "Building kernel from $SOURCES with $JOBS jobs"

rm -rf "$BUILD"
make -C "$SOURCES" O="$BUILD" bcm2712_defconfig
make -C "$SOURCES" O="$BUILD" -j"$JOBS" Image modules dtbs
make -C "$SOURCES" O="$BUILD" modules_install

mkdir -p /boot/overlays
cp "$BUILD/arch/arm64/boot/Image" /boot/kernel_2712.img
cp "$BUILD"/arch/arm64/boot/dts/broadcom/bcm2712*.dtb /boot/
cp "$BUILD"/arch/arm64/boot/dts/overlays/*.dtb* /boot/overlays/
[ ! -f "$SOURCES/arch/arm/boot/dts/overlays/README" ] || cp "$SOURCES/arch/arm/boot/dts/overlays/README" /boot/overlays/

echo "Installed kernel $(make -s -C "$SOURCES" O="$BUILD" kernelrelease) as /boot/kernel_2712.img"
rm -rf "$BUILD"

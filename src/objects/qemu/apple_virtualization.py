"""Native macOS backend for testing builds of the host architecture."""
from __future__ import annotations
import base64
import ctypes
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from gi.repository import GLib

from .qemu_run import QemuRun, QemuRunEvent, QemuBuildKind, QemuSupport, qemu_support


def apple_vm_library() -> str | None:
    if sys.platform != 'darwin':
        return None
    module = Path(__file__).resolve()
    candidates = [module.with_name('libcatalystlab-apple-vm.dylib'),
                  Path(sys.executable).resolve().parent.parent / 'Resources/native/libcatalystlab-apple-vm.dylib']
    if hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS) / 'native/libcatalystlab-apple-vm.dylib')
    # Development checkout: Meson builds the native bridge alongside its resources.
    if len(module.parents) > 3 and (module.parents[3] / 'meson.build').is_file():
        candidates += [module.parents[3] / folder / 'src/libcatalystlab-apple-vm.dylib'
                       for folder in ('.build-macos/build', '.build-macos-app/meson')]
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.R_OK)), None)


@dataclass
class AppleSupport:
    support: QemuSupport | None = None
    library: str | None = None
    limits: dict | None = None
    reason: str | None = None

    @property
    def available(self):
        return self.reason is None


class AppleVMBridge:
    """C ABI for the signed in-process Swift bridge. Loaded only on macOS."""
    Callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)

    def __init__(self, path):
        self.api = ctypes.CDLL(path)
        api = self.api
        api.cl_vm_probe.argtypes = []
        api.cl_vm_probe.restype = ctypes.c_void_p
        api.cl_vm_free.argtypes = [ctypes.c_void_p]
        api.cl_vm_free.restype = None
        api.cl_vm_create.argtypes = [ctypes.c_char_p, self.Callback, ctypes.c_void_p]
        api.cl_vm_create.restype = ctypes.c_void_p
        api.cl_vm_command.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        api.cl_vm_command.restype = None
        api.cl_vm_place.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_double] * 4
        api.cl_vm_place.restype = None
        api.cl_vm_release.argtypes = [ctypes.c_void_p]
        api.cl_vm_release.restype = None
        api.cl_vm_focus.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        api.cl_vm_focus.restype = None
        api.cl_vm_key.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool,
                                  ctypes.c_bool, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p]
        api.cl_vm_key.restype = None

    def probe(self):
        pointer = self.api.cl_vm_probe()
        if not pointer:
            raise RuntimeError('Apple Virtualization returned no capabilities')
        try:
            limits = json.loads(ctypes.string_at(pointer))
        finally:
            self.api.cl_vm_free(pointer)
        if 'error' in limits:
            raise RuntimeError(limits['error'])
        for key in ('min_cpus', 'max_cpus', 'min_memory', 'max_memory'):
            if not isinstance(limits[key], int) or limits[key] < 1:
                raise ValueError('Invalid resource limits from Apple Virtualization')
        return limits


@lru_cache(maxsize=8)
def _load_bridge(library: str, modified: int):
    return AppleVMBridge(library)


def _probe_library(library: str, modified: int) -> dict:
    return _load_bridge(library, modified).probe()


def apple_support(project_directory, build) -> AppleSupport:
    if sys.platform != 'darwin':
        return AppleSupport(reason='Apple Virtualization is available only on macOS')
    architecture = project_directory.get_architecture() if project_directory else None
    host = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'amd64': 'amd64'}.get(platform.machine().lower())
    if architecture is None or host is None or architecture.value != host:
        return AppleSupport(reason='Apple Virtualization requires a build matching this Mac’s CPU architecture')
    support = qemu_support(project_directory, build, check_runtime=False)
    if not support.available:
        return AppleSupport(support=support, reason=support.reason)
    support = replace(support, backend='apple', machine=replace(support.machine, console='hvc0'))
    if support.kind == QemuBuildKind.ISO:
        # EFI boot is required. A BIOS-only image cannot boot in Virtualization.framework.
        try:
            listing = subprocess.run(['bsdtar', '-tf', build.artifact_path], capture_output=True, text=True,
                                     timeout=30, check=True).stdout.lower()
            efi = 'bootaa64.efi' if host == 'arm64' else 'bootx64.efi'
            if efi not in listing and 'efi.img' not in listing and 'efiboot.img' not in listing:
                return AppleSupport(support=support, reason='Apple Virtualization requires an EFI-bootable ISO')
        except (OSError, subprocess.SubprocessError) as error:
            return AppleSupport(support=support, reason=f'Cannot inspect ISO boot files: {error}')
    library = apple_vm_library()
    if library is None:
        return AppleSupport(support=support, reason='The Apple Virtualization bridge is not installed; rebuild or update Catalyst Lab')
    try:
        limits = _probe_library(library, os.stat(library).st_mtime_ns)
        return AppleSupport(support, library, limits)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        detail = (getattr(error, 'stdout', '') or getattr(error, 'stderr', '') or str(error)).strip()
        return AppleSupport(support=support, reason=f'Apple Virtualization is unavailable: {detail}')


class AppleVirtualizationRun(QemuRun):
    """Shares the run registry with QEMU; VM and display live in the GTK process."""
    backend_name = 'Apple Virtualization'

    def __init__(self, project_directory, build, availability, **kwargs):
        super().__init__(project_directory, build, availability.support, **kwargs)
        self.availability = availability
        self.ready = False
        self.input_captured = False
        self._active = False
        self._handle = None
        self._bridge = None
        self._callback = None
        self._serial_received = False
        self._boot_output_timer = None

    @property
    def is_running(self):
        return self._active

    @property
    def accelerator(self):
        return 'Apple Virtualization'

    def _start_locked(self):
        try:
            current = apple_support(self.project_directory, self.build)
            if not current.available:
                raise RuntimeError(current.reason)
            limits = current.limits
            memory = self.memory_mib * 1024 * 1024
            if not limits['min_cpus'] <= self.cpus <= limits['max_cpus'] or not limits['min_memory'] <= memory <= limits['max_memory']:
                raise RuntimeError('CPU or memory allocation is outside the limits supported by this Mac')
            options = dict(name=self.name, cpus=self.cpus, memory=memory, directory=self.directory)
            if self.support.kind == QemuBuildKind.ISO:
                options['iso'] = self.build.artifact_path
            else:
                directory = self.support.image_directory
                if not directory:
                    raise RuntimeError('Temporary VM image directory has not been allocated')
                kernel_path = os.path.join(directory, self.support.boot_kernel)
                options.update(disk=os.path.join(directory, 'root.img'), kernel=kernel_path)
                if self.support.boot_initramfs:
                    options['initramfs'] = os.path.join(directory, self.support.boot_initramfs)
            self._bridge = _load_bridge(current.library, os.stat(current.library).st_mtime_ns)
            self._callback = AppleVMBridge.Callback(self._native_event)
            self._handle = self._bridge.api.cl_vm_create(json.dumps(options).encode(), self._callback, None)
            if not self._handle:
                raise RuntimeError('Could not create the Apple Virtualization VM')
        except Exception as error:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise
        self._active = True
        QemuRun.running_runs.append(self)
        QemuRun.running_runs_event_bus.emit(QemuRunEvent.RUNNING_RUNS_CHANGED)
        self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)

    def _native_event(self, context, message):
        # Copy the borrowed C string before the Swift callback returns.
        GLib.idle_add(self._handle_event, message.decode('utf-8', errors='replace'))

    def _handle_event(self, line):
        if not self._active:
            return False
        if line.startswith('SERIAL:'):
            text = base64.b64decode(line[7:]).decode('utf-8', errors='replace')
            self.serial_output = (self.serial_output + text)[-1024 * 1024:]
            if text:
                self._serial_received = True
                if self._boot_output_timer is not None:
                    GLib.source_remove(self._boot_output_timer)
                    self._boot_output_timer = None
            self.event_bus.emit(QemuRunEvent.SERIAL_OUTPUT, text)
        elif line == 'STATE:running':
            self.ready = True
            self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)
            if self.support.kind == QemuBuildKind.STAGE and not self._serial_received:
                self._boot_output_timer = GLib.timeout_add_seconds(30, self._report_missing_serial_output)
        elif line == 'INPUT:captured':
            self.input_captured = True
            self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)
        elif line == 'STATE:stopped':
            self._finish()
        elif line.startswith('DISK_PROGRESS:'):
            try:
                percent = max(0, min(100, int(line.partition(':')[2])))
            except ValueError:
                return False
            if self.output and self.output[-1].startswith('Preparing temporary disk image'):
                self.output[-1] = f'Preparing temporary disk image… {percent}%'
            self.event_bus.emit(QemuRunEvent.OUTPUT_LINE_ADDED, line)
        elif line.startswith('LAUNCH:'):
            detail = line.partition(':')[2].strip()
            self.output.append(detail)
            self.event_bus.emit(QemuRunEvent.OUTPUT_LINE_ADDED, detail)
        else:
            if line.startswith('ERROR:'):
                self.error = line[6:]
            self.output.append(line)
            self.event_bus.emit(QemuRunEvent.OUTPUT_LINE_ADDED, line)
        return False

    def _report_missing_serial_output(self):
        self._boot_output_timer = None
        if not self.is_running or not self.ready or self._serial_received:
            return False
        message = ("No guest serial output after 30 seconds. If the screen is blank, this image may require "
                   "hardware or firmware that Apple Virtualization does not emulate.")
        self.output.append(message)
        self.event_bus.emit(QemuRunEvent.OUTPUT_LINE_ADDED, message)
        return False

    def _finish(self):
        if self._boot_output_timer is not None:
            GLib.source_remove(self._boot_output_timer)
            self._boot_output_timer = None
        self.detach_display()
        self._active = False
        self.ready = False
        if self._handle:
            self._bridge.api.cl_vm_release(self._handle)
            self._handle = None
        if self in QemuRun.running_runs:
            QemuRun.running_runs.remove(self)
            QemuRun.running_runs_event_bus.emit(QemuRunEvent.RUNNING_RUNS_CHANGED)
        shutil.rmtree(self.directory, ignore_errors=True)
        from .qemu_run import cleanup_stage_image_directory
        cleanup_stage_image_directory(self.support.image_directory)
        self.event_bus.emit(QemuRunEvent.STATE_CHANGED, self)

    def place_display(self, window, x, y, width, height):
        if self._handle:
            self._bridge.api.cl_vm_place(self._handle, window, x, y, width, height)

    def detach_display(self):
        self.place_display(None, 0, 0, 0, 0)

    def focus_display(self, active):
        self.input_captured = active
        if self._handle:
            self._bridge.api.cl_vm_focus(self._handle, active)

    def send_key_event(self, keycode, pressed, is_modifier, flags, characters, unmodified_characters):
        if self._handle and self.input_captured:
            self._bridge.api.cl_vm_key(self._handle, keycode, pressed, is_modifier, flags,
                                       characters.encode('utf-8'), unmodified_characters.encode('utf-8'))

    def control(self, command):
        if self._handle:
            self._bridge.api.cl_vm_command(self._handle, command.encode())

    def send_serial(self, data):
        self.control('serial:' + base64.b64encode(data).decode('ascii'))

    def power_off(self):
        self.control('poweroff')

    def read_usage(self):
        # App-wide CPU/RSS includes GTK and other tasks; do not misreport it as VM usage.
        return None

    def read_disk_usage(self):
        if self.support.kind != QemuBuildKind.STAGE or not self.is_running:
            return None
        try:
            disk = os.stat(os.path.join(self.directory, 'root.img'))
            return disk.st_blocks * 512, disk.st_size
        except OSError:
            return None

    def stop(self):
        self.detach_display()
        self.control('stop')

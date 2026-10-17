"""macOS display integration check; run with .build-macos/venv/bin/python3-vm after ./run.sh.

Creates a short-lived GTK window and tests the native NSView without booting a guest.
"""
import ctypes, sys, types, tempfile, shutil, subprocess
from pathlib import Path
from unittest.mock import patch
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio
Adw.init()
root = Path(__file__).resolve().parents[1]
work = tempfile.TemporaryDirectory(prefix='catalystlab-embedding-')
library = str(Path(work.name) / 'embedding-test.dylib')
subprocess.run(['xcrun', 'swiftc', '-emit-library', '-module-cache-path', str(Path(work.name) / 'module-cache'),
                '-target', __import__('platform').machine() + '-apple-macos13.0',
                str(root / 'src/native/apple-vm.swift'), str(root / 'tests/native_embedding_fixture.swift'),
                '-o', library], check=True)
package = types.ModuleType('catalystlab')
package.__path__ = [str(root/'src')] + [str(p) for p in (root/'src').rglob('*') if p.is_dir()]
sys.modules['catalystlab'] = package
Gio.resources_register(Gio.Resource.load(str(root / '.build-macos/build/src/catalystlab.gresource')))
from catalystlab.apple_virtualization import AppleVMBridge, AppleVirtualizationRun, AppleSupport
from catalystlab.qemu_run import QemuSupport, QemuBuildKind, _MACHINES
from catalystlab import qemu_run_view
bridge = AppleVMBridge(library)
bridge.api.cl_vm_test_display.argtypes = [AppleVMBridge.Callback, ctypes.c_void_p]
bridge.api.cl_vm_test_display.restype = ctypes.c_void_p
bridge.api.cl_vm_test_parent.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_parent.restype = ctypes.c_bool
bridge.api.cl_vm_test_is_first_responder.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_is_first_responder.restype = ctypes.c_bool
bridge.api.cl_vm_test_keys.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_keys.restype = ctypes.c_int32
bridge.api.cl_vm_test_post_click.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_clicks.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_clicks.restype = ctypes.c_int32
bridge.api.cl_vm_test_post_escape.argtypes = [ctypes.c_void_p]
bridge.api.cl_vm_test_post_escape.restype = None
bridge.api.cl_vm_test_frame.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
project = types.SimpleNamespace(get_architecture=lambda: types.SimpleNamespace(value='arm64'))
build = types.SimpleNamespace(stage_name='Embedding test', artifact_path='/tmp/test', path='/tmp/test')
support = QemuSupport(QemuBuildKind.STAGE, _MACHINES['arm64'], kernel='Image', backend='apple')
availability = AppleSupport(support, library, {})
run = AppleVirtualizationRun(project, build, availability)
callback = AppleVMBridge.Callback(run._native_event)
run._bridge = bridge
run._handle = bridge.api.cl_vm_test_display(callback, None)
run._active = True
run.ready = True
with patch.object(qemu_run_view, 'apple_support', return_value=availability):
    view = qemu_run_view.QemuRunView(project, build, run=run)
window = Gtk.Window(title='Catalyst Lab embedding check', default_width=800, default_height=600)
window.set_child(view)
host_keys = []
shortcuts = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
    Gtk.CallbackAction.new(lambda *args: host_keys.append('Escape') or True)))
window.add_controller(shortcuts)
# VM startup should leave the embedded native view uncaptured until the user
# clicks inside it.
window.present()
loop = GLib.MainLoop()
errors = []
def fail(error):
    errors.append(error)
    loop.quit()
def first():
    try:
        assert bridge.api.cl_vm_test_parent(run._handle), 'Display did not attach'
        assert not view._native_input_active, 'Keyboard was captured automatically at startup'
        assert not bridge.api.cl_vm_test_is_first_responder(run._handle), (
            'AVF view unexpectedly took first-responder status at startup')
        frame = (ctypes.c_double * 4)()
        bridge.api.cl_vm_test_frame(run._handle, frame)
        assert frame[2] == view.native_display.get_width(), (list(frame),view.native_display.get_width())
        assert frame[3] == view.native_display.get_height(), (list(frame),view.native_display.get_height())
        assert 0 <= frame[0] < 800 and 0 <= frame[1] < 600, list(frame)
        print('Native NSView attached at', list(frame))
        window.set_default_size(1000, 720)
        GLib.timeout_add(250, resized)
    except BaseException as error: fail(error)
    return False
def resized():
    try:
        frame = (ctypes.c_double * 4)()
        bridge.api.cl_vm_test_frame(run._handle, frame)
        assert frame[2] == view.native_display.get_width() and frame[3] == view.native_display.get_height(), list(frame)
        assert frame[2] > 800, list(frame)
        print('Native NSView resized to', list(frame))
        bridge.api.cl_vm_test_post_click(run._handle)
        GLib.timeout_add(150, clicked)
    except BaseException as error: fail(error)
    return False
def clicked():
    try:
        assert view._native_input_active, f'Click did not capture keyboard (native clicks: {bridge.api.cl_vm_test_clicks(run._handle)})'
        assert bridge.api.cl_vm_test_clicks(run._handle) == 1, 'Mouse click did not reach native display'
        bridge.api.cl_vm_test_post_escape(run._handle)
        GLib.timeout_add(150, keyboard)
    except BaseException as error: fail(error)
    return False

def keyboard():
    try:
        assert bridge.api.cl_vm_test_keys(run._handle) == 1, 'Keyboard did not reach native VM view'
        assert not host_keys, 'GTK consumed VM keyboard shortcut'
        view.stack.set_visible_child_name('console')
        assert not bridge.api.cl_vm_test_parent(run._handle), 'Display remained over console'
        assert not view._native_input_active, 'Keyboard remained captured on console'
        view.stack.set_visible_child_name('screen')
        GLib.timeout_add(150, finish)
        print('Native keyboard forwarding bypasses GTK shortcuts; tab switching releases capture')
    except BaseException as error: fail(error)
    return False

def finish():
    try:
        assert bridge.api.cl_vm_test_parent(run._handle), 'Display did not reattach'
        view._detach_run()
        assert not bridge.api.cl_vm_test_parent(run._handle), 'Display remained after navigation'
        print('Console switching, reattachment and navigation detachment verified')
    except BaseException as error: errors.append(error)
    loop.quit()
    return False
GLib.timeout_add(300, first)
GLib.timeout_add_seconds(8, lambda: fail(AssertionError('Embedding test timed out')))
loop.run()
window.destroy()
bridge.api.cl_vm_release(run._handle)
shutil.rmtree(run.directory, ignore_errors=True)
work.cleanup()
if errors: raise errors[0]

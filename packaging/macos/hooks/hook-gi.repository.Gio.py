"""Collect GI's Gio typelib and platform GIO modules, including macOS dylibs."""
import glob
import os

from PyInstaller.utils.hooks.gi import GiModuleInfo


module_info = GiModuleInfo("Gio", "2.0")
if not module_info.available:
    raise RuntimeError("Gio 2.0 is required by Catalyst Lab")

binaries, datas, hiddenimports = module_info.collect_typelib_data()
hiddenimports.append("gi.repository.GioUnix")

# PyInstaller's stock hook only handles .so and .dll modules. Conda-forge's
# macOS packages use .dylib, so collect those from our isolated environment.
gio_module_dir = os.environ.get("CATALYSTLAB_GIO_MODULE_DIR")
if gio_module_dir and os.path.isdir(gio_module_dir):
    for pattern in ("*.dylib", "*.so", "*.dll"):
        binaries.extend((path, "gio_modules") for path in glob.glob(os.path.join(gio_module_dir, pattern)))
    cache = os.path.join(gio_module_dir, "giomodule.cache")
    if os.path.isfile(cache):
        datas.append((cache, "gio_modules"))

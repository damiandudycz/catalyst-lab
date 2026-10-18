"""Collect GTK 4's GI typelib and its runtime dependencies."""
from PyInstaller.utils.hooks.gi import GiModuleInfo


def hook(hook_api):
    # The stock PyInstaller hook defaults to GTK 3; take GTK 4 from spec's module-versions configuration.
    for name, version in (("Gtk", "3.0"), ("GdkMacos", "4.0")):
        module_info = GiModuleInfo(name, version, hook_api=hook_api)
        if not module_info.available:
            raise RuntimeError(f"{name} {module_info.version} is required by Catalyst Lab")

        binaries, datas, hiddenimports = module_info.collect_typelib_data()
        hook_api.add_datas(datas)
        hook_api.add_binaries(binaries)
        hook_api.add_imports(*hiddenimports)

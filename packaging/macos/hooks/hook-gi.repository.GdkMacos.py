"""Include GTK's native-window API used by the embedded VM display."""
from PyInstaller.utils.hooks.gi import GiModuleInfo


def hook(hook_api):
    module = GiModuleInfo('GdkMacos', '4.0')
    if not module.available:
        raise RuntimeError('GdkMacos 4.0 is required for the macOS VM display')
    binaries, datas, imports = module.collect_typelib_data()
    hook_api.add_binaries(binaries)
    hook_api.add_datas(datas)
    hook_api.add_imports(*imports)

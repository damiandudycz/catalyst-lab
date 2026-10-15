import importlib
import pkgutil

def scan_all_submodules(package_name: str):
    """Import all submodules under a given package to ensure decorators run."""
    package = importlib.import_module(package_name)
    for loader, name, is_pkg in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(name)

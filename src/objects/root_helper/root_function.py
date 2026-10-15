from __future__ import annotations
from typing import Callable
from functools import wraps

# ------------------------------------------------------------------------------
# @root_function decorator.
# ------------------------------------------------------------------------------

ROOT_FUNCTION_REGISTRY = {} # Registry for collecting root functions.

def root_function(func):
    """Registers a function and replaces it with a proxy that calls the root server."""
    """All these calls can throw in case server call fails to start."""
    ROOT_FUNCTION_REGISTRY[func.__name__] = func
    @wraps(func)
    def proxy_function(*args, **kwargs):
        from .root_helper_client import RootHelperClient
        return RootHelperClient.shared().call_root_function(
            func.__name__,
            *args,
            **kwargs
        )
    def _async(handler: Callable[[str],None] | None = None, completion_handler: callable | None = None, *args, **kwargs):
        from .root_helper_client import RootHelperClient
        return RootHelperClient.shared().call_root_function(
            func.__name__,
            *args,
            handler=handler,
            asynchronous=True,
            completion_handler=completion_handler,
            **kwargs
        )
    def _raw(*args, completion_handler: callable | None = None, **kwargs):
        from .root_helper_client import RootHelperClient
        return RootHelperClient.shared().call_root_function(
            func.__name__,
            *args,
            raw=True,
            completion_handler=completion_handler,
            **kwargs
        )
    def _async_raw(handler: Callable[[str],None] | None = None, completion_handler: callable | None = None, *args, **kwargs):
        from .root_helper_client import RootHelperClient
        return RootHelperClient.shared().call_root_function(
            func.__name__,
            *args,
            handler=handler,
            asynchronous=True,
            raw=True,
            completion_handler=completion_handler,
            **kwargs
        )
    # Attach variants
    proxy_function._async = _async
    proxy_function._raw = _raw
    proxy_function._async_raw = _async_raw
    return proxy_function

def local_for_rootless_paths(proxy: Callable, path_argument: str) -> Callable:
    """Root function that runs as user when path given in path_argument (keyword) is in rootless directory. Files there
    are owned by user (root of user namespace), so they don't need root helper."""
    original = ROOT_FUNCTION_REGISTRY[proxy.__name__]
    @wraps(proxy)
    def call(*args, **kwargs):
        from .rootless import is_rootless_path
        if is_rootless_path(kwargs.get(path_argument)):
            return original(*args, **kwargs)
        return proxy(*args, **kwargs)
    return call

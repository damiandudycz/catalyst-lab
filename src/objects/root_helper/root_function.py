from __future__ import annotations
from typing import Callable
from functools import wraps

# ------------------------------------------------------------------------------
# @root_function decorator.
# ------------------------------------------------------------------------------

ROOT_FUNCTION_REGISTRY = {} # Registry for collecting root functions.

def root_function(func):
    """Register a function and replace it with a proxy that calls the root server."""
    ROOT_FUNCTION_REGISTRY[func.__name__] = func

    def call(*args, handler=None, asynchronous=False, raw=False, completion_handler=None, **kwargs):
        from .root_helper_client import RootHelperClient
        return RootHelperClient.shared().call_root_function(
            func.__name__, *args, handler=handler, asynchronous=asynchronous, raw=raw,
            completion_handler=completion_handler, **kwargs
        )

    @wraps(func)
    def proxy_function(*args, **kwargs):
        return call(*args, **kwargs)

    def _async(handler: Callable[[str], None] | None = None, completion_handler: Callable | None = None,
               *args, **kwargs):
        return call(*args, handler=handler, asynchronous=True, completion_handler=completion_handler, **kwargs)

    def _raw(*args, completion_handler: Callable | None = None, **kwargs):
        return call(*args, raw=True, completion_handler=completion_handler, **kwargs)

    def _async_raw(handler: Callable[[str], None] | None = None, completion_handler: Callable | None = None,
                   *args, **kwargs):
        return call(*args, handler=handler, asynchronous=True, raw=True,
                    completion_handler=completion_handler, **kwargs)

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

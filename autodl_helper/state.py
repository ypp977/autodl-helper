from importlib import import_module as _import_module
import sys as _sys

_module = _import_module("autodl_helper.runtime.state")
_parent = _sys.modules.get(__name__.rpartition(".")[0])
if _parent is not None:
    setattr(_parent, "state", _module)
_sys.modules[__name__] = _module

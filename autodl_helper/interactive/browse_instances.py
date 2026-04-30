from __future__ import annotations

from .features.instances import *  # noqa: F401,F403

from .features.instances import __all__ as _feature_all
from .screen_support import _delegate

__all__ = list(_feature_all)

for _name in __all__:
    globals()[_name] = _delegate(_name, globals()[_name])

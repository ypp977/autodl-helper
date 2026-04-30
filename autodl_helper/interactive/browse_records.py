from __future__ import annotations

from .features.accounts import *  # noqa: F401,F403
from .features.history import *  # noqa: F401,F403
from .screen_support import _delegate

from .features.accounts import __all__ as _accounts_all
from .features.history import __all__ as _history_all

__all__ = [*_accounts_all, *_history_all]

for _name in __all__:
    globals()[_name] = _delegate(_name, globals()[_name])

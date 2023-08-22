import pytest
from typing import Any


class UnknownConfigOptionError(Exception):
    pass


class _Defaults:
    _DEFAULT_CONFIG = {
        "RETRIES": 1,
        "DELAY": 0,
        "CUMULATIVE_TIMING": False,
        "FILTERED_EXCEPTIONS": (),
        "EXCLUDED_EXCEPTIONS": (),
    }

    def __init__(self) -> None:
        object.__setattr__(self, "_opts", self._DEFAULT_CONFIG.copy())

    def __getattr__(self, name: str) -> Any:
        if name in self._opts:
            return self._opts[name]
        raise UnknownConfigOptionError(f"{name} is not a valid option!")

    def __setattr__(self, name: str, value: Any) -> None:
        try:
            object.__getattribute__(self, "_opts")[name] = value
        except KeyError:
            raise UnknownConfigOptionError(f"{name} is not a valid option!")

    def configure(self, config: pytest.Config) -> None:
        for key in self._opts:
            self._opts[key] = config.getoption(key.lower(), self._DEFAULT_CONFIG[key])


Defaults = _Defaults()

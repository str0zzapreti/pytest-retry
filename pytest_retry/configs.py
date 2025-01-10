import pytest
from typing import Any

RETRIES = "RETRIES"
RETRY_DELAY = "RETRY_DELAY"
CUMULATIVE_TIMING = "CUMULATIVE_TIMING"
RETRY_OUTCOME = "RETRY_OUTCOME"


class UnknownDefaultError(Exception):
    pass


class _Defaults:
    _DEFAULT_CONFIG = {
        RETRIES: 1,  # A flaky mark with 0 args should default to 1 retry.
        RETRY_DELAY: 0,
        CUMULATIVE_TIMING: False,
        RETRY_OUTCOME: "retried",  # The string to use for retry outcomes
    }

    def __init__(self) -> None:
        object.__setattr__(self, "_opts", self._DEFAULT_CONFIG.copy())

    def __getattr__(self, name: str) -> Any:
        if name in self._opts:
            return self._opts[name]
        raise UnknownDefaultError(f"{name} is not a valid default option!")

    def __setattr__(self, name: str, value: Any) -> None:
        raise ValueError("Defaults cannot be overwritten manually! Please use `configure()`")

    def add(self, name: str, value: Any) -> None:
        if name in self._opts:
            raise ValueError(f"{name} is already an existing default!")
        self._opts[name] = value

    def load_ini(self, config: pytest.Config) -> None:
        """
        Pytest has separate methods for loading command line args and ini options. All ini
        values are stored as strings so must be converted to the proper type.
        """
        self._opts[RETRIES] = int(config.getini(RETRIES.lower()))
        self._opts[RETRY_DELAY] = float(config.getini(RETRY_DELAY.lower()))
        self._opts[CUMULATIVE_TIMING] = config.getini(CUMULATIVE_TIMING.lower())
        self._opts[RETRY_OUTCOME] = config.getini(RETRY_OUTCOME.lower())

    def configure(self, config: pytest.Config) -> None:
        if config.getini("retries"):
            self.load_ini(config)
        for key in self._opts:
            if (val := config.getoption(key.lower())) is not None:
                self._opts[key] = val


Defaults = _Defaults()

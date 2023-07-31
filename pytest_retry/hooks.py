import pytest


@pytest.hookspec(firstresult=True)
def pytest_set_filtered_exceptions() -> None:
    """
    Return a collection of exception classes to be used as a filter when retrying tests.

    This pytest hook is called during setup to gather a collection of exception classes.
    Only tests that fail with one of the listed exceptions will be retried (individual flaky
    marks which specify their own exceptions will override this list).

    Example:
        # In your conftest.py file:
        def pytest_set_filtered_exceptions():
            return (CustomError, ValueError)
    """
    ...


@pytest.hookspec(firstresult=True)
def pytest_set_excluded_exceptions() -> None:
    """
    Return a collection of exception classes to be excluded when retrying tests.

    This pytest hook is called during setup to gather a collection of exception classes.
    Tests that fail with one of the listed exceptions will NOT be retried (individual flaky
    marks which specify their own exceptions will override this list).

    Example:
        # In your conftest.py file:
        def pytest_set_filtered_exceptions():
            return (CustomError, ValueError)
    """
    ...

import pytest
import bdb
from time import sleep
from io import StringIO
from traceback import format_exception
from typing import Generator, Optional
from collections.abc import Iterable
from pytest_retry.configs import Defaults
from _pytest.terminal import TerminalReporter
from _pytest.logging import caplog_records_key


outcome_key = pytest.StashKey[str]()
attempts_key = pytest.StashKey[int]()
duration_key = pytest.StashKey[float]()
stages = ("setup", "call", "teardown")


class ConfigurationError(Exception):
    pass


class ExceptionFilter:
    """
    Helper class which returns a bool when called based on the filter type (expected or excluded)
    and whether the exception exists within the list
    """

    def __init__(self, expected_exceptions: Iterable, excluded_exceptions: Iterable):
        if expected_exceptions and excluded_exceptions:
            raise ConfigurationError(
                "filtered_exceptions and excluded_exceptions are exclusive and cannot "
                "be defined simultaneously."
            )
        self.list_type = bool(expected_exceptions)
        self.filter = expected_exceptions or excluded_exceptions or []

    def __call__(self, exception_type: Optional[type[BaseException]]) -> bool:
        try:
            return not self.filter or bool(self.list_type == bool(exception_type in self.filter))
        except TypeError:
            raise ConfigurationError(
                "Filtered or excluded exceptions must be passed as a collection. If using the "
                "flaky mark, this means `only_on` or `exclude` args must be a collection too."
            )

    def __bool__(self) -> bool:
        return bool(self.filter)


class RetryHandler:
    """
    Stores statistics and reports for flaky tests and fixtures which have
    failed at least once during the test session and need to be retried
    """

    def __init__(self) -> None:
        self.stream = StringIO()
        self.trace_limit: Optional[int] = -1
        self.node_stats: dict[str, dict] = {}
        self.messages = (
            " failed on attempt {attempt}! Retrying!\n\t",
            " failed after {attempt} attempts!\n\t",
            " teardown failed on attempt {attempt}! Exiting immediately!\n\t",
        )

    def log_attempt(
        self, attempt: int, name: str, exc: Optional[pytest.ExceptionInfo], outcome: int
    ) -> None:
        message = self.messages[outcome].format(attempt=attempt)
        err = (exc.type, exc.value, exc.tb)  # type: ignore
        formatted_trace = (
            "".join(format_exception(*err, limit=self.trace_limit)).replace("\n", "\n\t").rstrip()
        )
        self.stream.writelines([f"\t{name}", message, formatted_trace, "\n\n"])

    def add_retry_report(self, terminalreporter: TerminalReporter) -> None:
        contents = self.stream.getvalue()
        if not contents:
            return

        terminalreporter.write("\n")
        terminalreporter.section(
            "the following tests were retried", sep="=", bold=True, yellow=True
        )
        terminalreporter.write(contents)
        terminalreporter.section("end of test retry report", sep="=", bold=True, yellow=True)
        terminalreporter.write("\n")

    def record_node_stats(self, report: pytest.TestReport) -> None:
        self.node_stats[report.nodeid]["outcomes"][report.when].append(report.outcome)
        self.node_stats[report.nodeid]["durations"][report.when].append(report.duration)

    def simple_outcome(self, item: pytest.Item) -> str:
        """
        Return failed if setup, teardown, or final call outcome is 'failed'
        Return skipped if test was skipped
        """
        test_outcomes = self.node_stats[item.nodeid]["outcomes"]
        for outcome in ("skipped", "failed"):
            if outcome in test_outcomes["setup"]:
                return outcome
        if not test_outcomes["call"] or test_outcomes["call"][-1] == "failed":
            return "failed"
        if "failed" in test_outcomes["teardown"]:
            return "failed"
        return "passed"

    def simple_duration(self, item: pytest.Item) -> float:
        """
        Return total duration for test summing setup, teardown, and final call
        """
        return sum(self.node_stats[item.nodeid]["durations"][stage][-1] for stage in stages)

    def sum_attempts(self, item: pytest.Item) -> int:
        return len(self.node_stats[item.nodeid]["outcomes"]["call"])


retry_manager = RetryHandler()


def has_interactive_exception(call: pytest.CallInfo) -> bool:
    # Check whether the call raised an exception that should be reported as interactive.
    if call.excinfo is None:
        # Didn't raise.
        return False
    if isinstance(call.excinfo.value, bdb.BdbQuit):
        # Special control flow exception.
        return False
    return True


def should_handle_retry(rep: pytest.TestReport) -> bool:
    # if test passed, don't retry
    if rep.passed:
        return False
    # if teardown stage, don't retry
    if rep.when == "teardown":
        return False
    # if test was skipped, don't retry
    if rep.skipped:
        return False
    # if test is xfail, don't retry
    if hasattr(rep, "wasxfail"):
        return False
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Optional[object]:
    retry_manager.node_stats[item.nodeid] = {
        "outcomes": {k: [] for k in stages},
        "durations": {k: [0.0] for k in stages},
    }
    yield
    item.stash[outcome_key] = retry_manager.simple_outcome(item)
    item.stash[duration_key] = retry_manager.simple_duration(item)  # always overwrite, for now
    item.stash[attempts_key] = retry_manager.sum_attempts(item)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo
) -> Generator[None, pytest.TestReport, None]:
    outcome = yield
    original_report: pytest.TestReport = outcome.get_result()
    retry_manager.record_node_stats(original_report)
    # Set dynamic outcome for each stage until runtest protocol has completed.
    item.stash[outcome_key] = original_report.outcome
    if not should_handle_retry(original_report):
        return

    flake_mark = item.get_closest_marker("flaky")
    if flake_mark is None:
        return

    condition = flake_mark.kwargs.get("condition")
    if condition is False:
        return

    exception_filter = ExceptionFilter(
        flake_mark.kwargs.get("only_on", []),
        flake_mark.kwargs.get("exclude", []),
    ) or ExceptionFilter(Defaults.FILTERED_EXCEPTIONS, Defaults.EXCLUDED_EXCEPTIONS)
    if not exception_filter(call.excinfo.type):  # type: ignore
        return

    retries = flake_mark.kwargs.get("retries", Defaults.RETRIES)
    delay = flake_mark.kwargs.get("delay", Defaults.DELAY)
    cumulative_timing = flake_mark.kwargs.get("cumulative_timing", Defaults.CUMULATIVE_TIMING)
    attempts = 1
    hook = item.ihook

    while True:
        if call.when == "setup":
            break  # will handle fixture setup retries in v2, if necessary. For now, this is fine.
        # Default teardowns are already excluded, so this must be the `call` stage
        # Try preliminary teardown using a fake item to ensure every local fixture (i.e.
        # excluding session) is torn down. Yes, including module and class fixtures
        t_call = pytest.CallInfo.from_call(
            lambda: hook.pytest_runtest_teardown(
                item=item,
                nextitem=pytest.Item.from_parent(item.session, name="fakeboi"),
            ),
            when="teardown",
        )
        # If teardown fails, break. Flaky teardowns are not acceptable and should raise immediately
        if t_call.excinfo:
            item.stash[outcome_key] = "failed"
            retry_manager.log_attempt(
                attempt=attempts, name=item.name, exc=t_call.excinfo, outcome=2
            )
            # Prevents a KeyError when an error during retry teardown causes a redundant teardown
            item.stash[caplog_records_key] = {}  # type: ignore
            break

        # If teardown passes, send report that the test is being retried
        if attempts == 1:
            original_report.outcome = "retried"  # type: ignore
            hook.pytest_runtest_logreport(report=original_report)
            original_report.outcome = "failed"
        retry_manager.log_attempt(attempt=attempts, name=item.name, exc=call.excinfo, outcome=0)
        sleep(delay)
        # Call _initrequest(). Only way to get the setup to run again
        item._initrequest()  # type: ignore[attr-defined]

        pytest.CallInfo.from_call(lambda: hook.pytest_runtest_setup(item=item), when="setup")
        call = pytest.CallInfo.from_call(lambda: hook.pytest_runtest_call(item=item), when="call")
        retry_report = pytest.TestReport.from_item_and_call(item, call)
        retry_manager.record_node_stats(retry_report)

        # Do the exception interaction step
        # (may not bother to support this since this is designed for automated runs, not debugging)
        if has_interactive_exception(call):
            hook.pytest_exception_interact(node=item, call=call, report=retry_report)

        attempts += 1
        should_keep_retrying = (
            not retry_report.passed
            and attempts <= retries
            and exception_filter(call.excinfo.type)  # type: ignore
        )

        if not should_keep_retrying:
            original_report.outcome = retry_report.outcome
            original_report.longrepr = retry_report.longrepr
            if cumulative_timing is False:
                original_report.duration = retry_report.duration
            else:
                original_report.duration += retry_report.duration

            if retry_report.failed:
                retry_manager.log_attempt(
                    attempt=attempts, name=item.name, exc=call.excinfo, outcome=1
                )
            break


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    retry_manager.add_retry_report(terminalreporter)


def pytest_report_teststatus(
    report: pytest.TestReport,
) -> Optional[tuple[str, str, tuple[str, dict]]]:
    if report.outcome == "retried":
        return "retried", "R", ("RETRY", {"yellow": True})
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "flaky(retries=1, delay=0, only_on=..., exclude=..., condition=...): indicate a flaky "
        "test which will be retried the number of times specified with an (optional) specified "
        "delay between each attempt. Collections of one or more exceptions can be passed so "
        "that the test is retried only on those exceptions, or excluding those exceptions. "
        "Any statement which returns a bool can be used as a condition",
    )
    if config.getoption("verbose"):
        # if pytest config has -v enabled, then don't limit traceback length
        retry_manager.trace_limit = None
    Defaults.configure(config)
    Defaults.FILTERED_EXCEPTIONS = config.hook.pytest_set_filtered_exceptions() or []
    Defaults.EXCLUDED_EXCEPTIONS = config.hook.pytest_set_excluded_exceptions() or []


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup(
        "pytest-retry", "retry flaky tests to compensate for intermittent failures"
    )
    group.addoption(
        "--retries",
        action="store",
        dest="retries",
        type=int,
        default=0,
        help="number of times to retry failed tests. Defaults to 0.",
    )
    group.addoption(
        "--retry-delay",
        action="store",
        dest="delay",
        type=float,
        default=0,
        help="add a delay (in seconds) between retries.",
    )
    group.addoption(
        "--cumulative-timing",
        action="store",
        dest="cumulative_timing",
        type=bool,
        default=False,
        help="if True, retry duration will be included in overall reported test duration",
    )


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """This example assumes the hooks are grouped in the 'sample_hook' module."""
    from pytest_retry import hooks

    pluginmanager.add_hookspecs(hooks)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--retries"):
        return
    flaky = pytest.mark.flaky(retries=config.option.retries)
    for item in items:
        if "flaky" not in item.keywords:
            item.add_marker(flaky)

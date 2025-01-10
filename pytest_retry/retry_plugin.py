import pytest
import bdb
from time import sleep
from logging import LogRecord
from traceback import format_exception
from typing import Any, Generator, Optional
from collections.abc import Iterable
from pytest_retry.configs import Defaults
from pytest_retry.server import ReportHandler, OfflineReporter, ReportServer, ClientReporter
from _pytest.terminal import TerminalReporter
from _pytest.logging import caplog_records_key


outcome_key = pytest.StashKey[str]()
attempts_key = pytest.StashKey[int]()
duration_key = pytest.StashKey[float]()
server_port_key = pytest.StashKey[int]()
stages = ("setup", "call", "teardown")
RETRY = 0
FAIL = 1
EXIT = 2
PASS = 3


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


class RetryManager:
    """
    Stores statistics and reports for flaky tests and fixtures which have
    failed at least once during the test session and need to be retried
    """

    def __init__(self) -> None:
        self.reporter: ReportHandler = OfflineReporter()
        self.trace_limit: Optional[int] = 1
        self.node_stats: dict[str, dict] = {}
        self.messages = (
            " failed on attempt {attempt}! Retrying!\n\t",
            " failed after {attempt} attempts!\n\t",
            " teardown failed on attempt {attempt}! Exiting immediately!\n\t",
            " passed on attempt {attempt}!\n\t",
        )

    def log_attempt(
        self, attempt: int, name: str, exc: Optional[pytest.ExceptionInfo], result: int
    ) -> None:
        message = self.messages[result].format(attempt=attempt)
        formatted_trace = ""
        if exc:
            err = (exc.type, exc.value, exc.tb)
            formatted_trace = (
                formatted_trace.join(format_exception(*err, limit=self.trace_limit))
                .replace("\n", "\n\t")
                .rstrip()
            )
        self.reporter.record_attempt([f"\t{name}", message, formatted_trace, "\n\n"])

    def build_retry_report(self, terminal_reporter: TerminalReporter) -> None:
        contents = self.reporter.stream.getvalue()
        if not contents:
            return

        terminal_reporter.write("\n")
        terminal_reporter.section(
            "the following tests were retried", sep="=", bold=True, yellow=True
        )
        terminal_reporter.write(contents)
        terminal_reporter.section("end of test retry report", sep="=", bold=True, yellow=True)
        terminal_reporter.write("\n")

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
        # can probably just simplify this to return test_outcomes["teardown"] as a fallthrough
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


retry_manager = RetryManager()


def has_interactive_exception(call: pytest.CallInfo) -> bool:
    if call.excinfo is None:
        return False
    if isinstance(call.excinfo.value, bdb.BdbQuit):
        # Special control flow exception.
        return False
    return True


def should_handle_retry(call: pytest.CallInfo) -> bool:
    if call.excinfo is None:
        return False
    # if teardown stage, don't retry
    # may handle fixture setup retries in v2 if requested. For now, this is fine.
    if call.when in {"setup", "teardown"}:
        return False
    # if test was skipped, don't retry
    if call.excinfo.typename == "Skipped":
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
    # Set dynamic outcome for each stage until runtest protocol has completed
    item.stash[outcome_key] = original_report.outcome

    if not should_handle_retry(call):
        return
    # xfail tests don't raise a Skipped exception if they fail, but are still marked as skipped
    if original_report.skipped is True:
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
    delay = flake_mark.kwargs.get("delay", Defaults.RETRY_DELAY)
    cumulative_timing = flake_mark.kwargs.get("cumulative_timing", Defaults.CUMULATIVE_TIMING)
    attempts = 1
    hook = item.ihook

    while True:
        # Default teardowns are already excluded, so this must be the `call` stage
        # Try preliminary teardown using a fake class to ensure every local fixture (i.e.
        # excluding session) is torn down. Yes, including module and class fixtures
        t_call = pytest.CallInfo.from_call(
            lambda: hook.pytest_runtest_teardown(
                item=item,
                nextitem=pytest.Class.from_parent(item.session, name="Fakeboi"),
            ),
            when="teardown",
        )
        # If teardown fails, break. Flaky teardowns are unacceptable and should raise immediately
        if t_call.excinfo:
            item.stash[outcome_key] = "failed"
            retry_manager.log_attempt(
                attempt=attempts, name=item.name, exc=t_call.excinfo, result=EXIT
            )
            # Prevents a KeyError when an error during retry teardown causes a redundant teardown
            empty: dict[str, list[LogRecord]] = {}
            item.stash[caplog_records_key] = empty
            break

        # If teardown passes, send report that the test is being retried
        if attempts == 1:
            original_report.outcome = Defaults.RETRY_OUTCOME  # type: ignore
            hook.pytest_runtest_logreport(report=original_report)
            original_report.outcome = "failed"
        retry_manager.log_attempt(attempt=attempts, name=item.name, exc=call.excinfo, result=RETRY)
        sleep(delay)
        # Calling _initrequest() is required to reset fixtures for a retry. Make public pls?
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
                original_report.duration = sum(
                    retry_manager.node_stats[original_report.nodeid]["durations"]["call"]
                )

            retry_manager.log_attempt(
                attempt=attempts,
                name=item.name,
                exc=call.excinfo,
                result=FAIL if retry_report.failed else PASS,
            )
            break


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    retry_manager.build_retry_report(terminalreporter)


def pytest_report_teststatus(
    report: pytest.TestReport,
) -> Optional[tuple[str, str, tuple[str, dict]]]:
    if report.outcome == Defaults.RETRY_OUTCOME:
        return Defaults.RETRY_OUTCOME, "R", ("RETRY", {"yellow": True})
    return None


class XdistHook:
    @staticmethod
    def pytest_configure_node(node: Any) -> None:  # Xdist WorkerController instance
        # Tells each worker node which port was randomly assigned to the retry server
        node.workerinput["server_port"] = node.config.stash[server_port_key]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "flaky(retries=1, delay=0, only_on=..., exclude=..., condition=...): indicate a flaky "
        "test which will be retried the number of times specified with an (optional) specified "
        "delay between each attempt. Collections of one or more exceptions can be passed so "
        "that the test is retried only on those exceptions, or excluding those exceptions. "
        "Any statement which returns a bool can be used as a condition",
    )
    verbosity = config.getoption("verbose")
    if verbosity:
        # set trace limit according to verbosity count, or unlimited if 5
        retry_manager.trace_limit = verbosity if verbosity < 5 else None
    Defaults.configure(config)
    Defaults.add("FILTERED_EXCEPTIONS", config.hook.pytest_set_filtered_exceptions() or [])
    Defaults.add("EXCLUDED_EXCEPTIONS", config.hook.pytest_set_excluded_exceptions() or [])
    if config.pluginmanager.has_plugin("xdist") and config.getoption("numprocesses", False):
        config.pluginmanager.register(XdistHook())
        retry_manager.reporter = ReportServer()
        config.stash[server_port_key] = retry_manager.reporter.initialize_server()
    elif hasattr(config, "workerinput"):
        # pytest-xdist doesn't use the config stash, so have to ignore a type error here
        retry_manager.reporter = ClientReporter(config.workerinput["server_port"])  # type: ignore


RETRIES_HELP_TEXT = "number of times to retry failed tests. Defaults to 0."
DELAY_HELP_TEXT = "configure a delay (in seconds) between retries."
TIMING_HELP_TEXT = "if True, retry duration will be included in overall reported test duration"
RETRY_HELP_TEXT = "configure the outcome of retried tests. Defaults to 'retried'"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup(
        "pytest-retry", "retry flaky tests to compensate for intermittent failures"
    )
    group.addoption(
        "--retries",
        action="store",
        dest="retries",
        type=int,
        help=RETRIES_HELP_TEXT,
    )
    group.addoption(
        "--retry-delay",
        action="store",
        dest="retry_delay",
        type=float,
        help=DELAY_HELP_TEXT,
    )
    group.addoption(
        "--cumulative-timing",
        action="store",
        dest="cumulative_timing",
        type=bool,
        help=TIMING_HELP_TEXT,
    )
    group.addoption(
        "--retry-outcome",
        action="store",
        dest="retry_outcome",
        type=str,
        help=RETRY_HELP_TEXT,
    )
    parser.addini("retries", RETRIES_HELP_TEXT, default=0, type="string")
    parser.addini("retry_delay", DELAY_HELP_TEXT, default=0, type="string")
    parser.addini("cumulative_timing", TIMING_HELP_TEXT, default=False, type="bool")
    parser.addini("retry_outcome", RETRY_HELP_TEXT, default="retried")


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    """This example assumes the hooks are grouped in the 'sample_hook' module."""
    from pytest_retry import hooks

    pluginmanager.add_hookspecs(hooks)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not (config.getoption("--retries") or config.getini("retries")):
        return
    flaky = pytest.mark.flaky(retries=Defaults.RETRIES)
    for item in items:
        if "flaky" not in item.keywords:
            item.add_marker(flaky)

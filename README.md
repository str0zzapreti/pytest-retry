![Tests](https://github.com/str0zzapreti/pytest-retry/actions/workflows/tests.yaml/badge.svg)
# pytest-retry

pytest-retry is a plugin for Pytest which adds the ability to retry flaky tests,
thereby improving the consistency of the test suite results. 

## Requirements

pytest-retry is designed for the latest versions of Python and Pytest. Python 3.9+
and pytest 7.0.0 are required. 

## Installation

Use pip to install pytest-retry:
```
$ pip install pytest-retry
```

## Usage

There are two main ways to use pytest-retry:

### 1. Command line

Run Pytest with the command line argument --retries in order to retry every test in 
the event of a failure. The following example will retry each failed up to two times
before proceeding to the next test:

```
$ python -m pytest --retries 2
```

An optional delay can be specified using the --retry-delay argument. This will insert
a fixed delay (in seconds) between each attempt when a test fails. This can be useful
if the test failures are due to intermittent environment issues which clear up after
a few seconds

```
$ python -m pytest --retries 2 --retry-delay 5
```

#### Advanced Options:
There are two custom hooks provided for the purpose of setting global exception
filters for your entire Pytest suite. `pytest_set_filtered_exceptions`
and `pytest_set_excluded_exceptions`. You can define either of them in your 
conftest.py file and return a list of exception types. Note: these hooks are 
mutually exclusive and cannot both be defined at the same time.

Example:
```
def pytest_set_excluded_exceptions():
    """
    All tests will be retried unless they fail due to an AssertionError or CustomError
    """
    return [AssertionError, CustomError]
```

There is a command line option to specify the test timing method, which can either
be `overwrite` (default) or `cumulative`. With cumulative timing, the duration of 
each test attempt is summed for the reported overall test duration. The default
behavior simply reports the timing of the final attempt.

```
$ python -m pytest --retries 2 --cumulative-timing 1
```

If you're not sure which to use, stick with the default `overwrite` method. This
generally plays nicer with time-based test splitting algorithms and will result in
more even splits. 

### 2. Pytest flaky mark

Mark individual tests as 'flaky' to retry them when they fail. If no command line
arguments are passed, only the marked tests will be retried. The default values
are 1 retry attempt with a 0-second delay

```
@pytest.mark.flaky
def test_unreliable_service():
    ...
```

The number of times each test will be retried and/or the delay can be manually
specified as well

```
@pytest.mark.flaky(retries=3, delay=1)
def test_unreliable_service():
    # This test will be retried up to 3 times (4 attempts total) with a
    # one second delay between each attempt
    ...
```

If you want to control filtered or excluded exceptions per-test, the flaky mark
provides the `only_on` and `exclude` arguments which both take a list of exception
types, including any custom types you may have defined for your project. Note that 
only one of these arguments may be used at a time.

A test with a list of `only_on` exceptions will only be retried if it fails with
one of the listed exceptions. A test with a list of `exclude` exceptions will
only be retried if it fails with an exception which does not match any of the
listed exceptions.

If the exception for a subsequent attempt changes and no longer matches the filter,
no further attempts will be made and the test will immediately fail.

```
@pytest.mark.flaky(retries=2, only_on=[ValueError, IndexError])
def test_unreliable_service():
    # This test will only be retried if it fails due to raising a ValueError
    # or an IndexError. e.g., an AssertionError will fail without retrying
    ...
```

If you want some other generalized condition to control whether a test is retried, use the
`condition` argument. Any statement which results in a bool can be used here to add granularity
to your retries. The test will only be retried if `condition` is `True`. Note, there is no
matching command line option for `condition`, but if you need to globally apply this type of logic
to all of your tests, consider invoking the `pytest_collection_modifyitems` hook.

```
@pytest.mark.flaky(retries=2, condition=sys.platform.startswith('win32'))
def test_only_flaky_on_some_systems():
    # This test will only be retried if sys.platform.startswith('win32') evaluates to `True`
```

Finally, there is a flaky mark argument for the test timing method, which can either
be `overwrite` (default) or `cumulative`. See **Command Line** > **Advanced Options** 
for more information

```
@pytest.mark.flaky(timing='overwrite')
def test_unreliable_service():
    ...
```

A flaky mark will override any command line options and exception filter hooks
specified when running Pytest.

### Things to consider

- **Currently, failing test fixtures are not retried.** In the future, flaky test setup 
may be retried, although given the undesirability of flaky tests in general, flaky setup 
should be avoided at all costs. Any failures during teardown will immediately halt
further attempts so that they can be addressed immediately. Make sure your teardowns
always work reliably regardless of the number of retries when using this plugin

- When a flaky test is retried, the plugin runs teardown steps for the test as if it 
had passed. This is to ensure that any partial state created by the test is cleaned up 
before the next attempt so that subsequent attempts do not conflict with one another.
Class and module fixtures are included in this teardown with the assumption that false
test failures should be a rare occurrence and the performance hit from re-running 
these potentially expensive fixtures is worth it to ensure clean initial test state. 
With feedback, the option to not re-run class and module fixtures may be added, but 
in general, these types of fixtures should be avoided for known flaky tests.

- Flaky tests are not sustainable. This plugin is designed as an easy short-term
solution while a permanent fix is implemented. Use the reports generated by this plugin
to identify issues with the tests or testing environment and resolve them.

## Reporting

pytest-retry intercepts the standard Pytest report flow in order to retry tests and
update the reports as required. When a test is retried at least once, an R is printed
to the live test output and the counter of retried tests is incremented by 1. After
the test session has completed, an additional report is generated below the standard
output which lists all of the tests which were retried, along with the exceptions
that occurred during each failed attempt. 

```
plugins: retry-1.1.0
collected 1 item

test_retry_passes_after_temporary_test_failure.py R.                     [100%]

======================= the following tests were retried =======================

	test_eventually_passes failed on attempt 1! Retrying!
	Traceback (most recent call last):
	  File "tests/test_example.py", line 4, in test_eventually_passes
	    assert len(a) > 1
	AssertionError: assert 1 > 1
	 +  where 1 = len([1])

=========================== end of test retry report ===========================


========================= 1 passed, 1 retried in 0.01s =========================
```

Tests which have been retried but eventually pass are counted as both retried and
passed, and tests which have been retried but eventually fail are counted as both
retried and failed. Skipped, xfailed, and xpassed tests are never retried.

Three pytest stash keys are available to import from the pytest_retry plugin:
`attempts_key`, `outcome_key`, and `duration_key`. These keys are used by the plugin
to store the number of attempts each item has undergone, whether the test passed or
failed, and the total duration from setup to teardown, respectively. (If any stage of 
setup, call, or teardown fails, a test is considered failed overall). These stash keys 
can be used to retrieve these reports for use in your own hooks or plugins.

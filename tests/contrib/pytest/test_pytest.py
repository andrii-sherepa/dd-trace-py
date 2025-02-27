import json
import os
import sys

import mock
import pytest

import ddtrace
from ddtrace.constants import ERROR_MSG
from ddtrace.constants import SAMPLING_PRIORITY_KEY
from ddtrace.contrib.pytest.constants import XFAIL_REASON
from ddtrace.contrib.pytest.plugin import is_enabled
from ddtrace.ext import ci
from ddtrace.ext import git
from ddtrace.ext import test
from ddtrace.internal import compat
from ddtrace.internal.ci_visibility import CIVisibility
from ddtrace.internal.ci_visibility.constants import COVERAGE_TAG_NAME
from ddtrace.internal.ci_visibility.encoder import CIVisibilityEncoderV01
from ddtrace.internal.compat import PY2
from tests.ci_visibility.test_encoder import _patch_dummy_writer
from tests.utils import TracerTestCase
from tests.utils import override_env
from tests.utils import override_global_config


class PytestTestCase(TracerTestCase):
    @pytest.fixture(autouse=True)
    def fixtures(self, testdir, monkeypatch, git_repo):
        self.testdir = testdir
        self.monkeypatch = monkeypatch
        self.git_repo = git_repo

    def inline_run(self, *args):
        """Execute test script with test tracer."""

        class CIVisibilityPlugin:
            @staticmethod
            def pytest_configure(config):
                if is_enabled(config):
                    with _patch_dummy_writer():
                        assert CIVisibility.enabled
                        CIVisibility.disable()
                        CIVisibility.enable(tracer=self.tracer, config=ddtrace.config.pytest)

        with override_env(dict(DD_API_KEY="foobar.baz")):
            return self.testdir.inline_run(*args, plugins=[CIVisibilityPlugin()])

    def subprocess_run(self, *args):
        """Execute test script with test tracer."""
        return self.testdir.runpytest_subprocess(*args)

    @pytest.mark.skipif(sys.version_info[0] == 2, reason="Triggers a bug with coverage, sqlite and Python 2")
    def test_patch_all(self):
        """Test with --ddtrace-patch-all."""
        py_file = self.testdir.makepyfile(
            """
            import ddtrace

            def test_patched_all():
                assert ddtrace._monkey._PATCHED_MODULES
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace-patch-all", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 0

    @pytest.mark.skipif(sys.version_info[0] == 2, reason="Triggers a bug with coverage, sqlite and Python 2")
    def test_patch_all_init(self):
        """Test with ddtrace-patch-all via ini."""
        self.testdir.makefile(".ini", pytest="[pytest]\nddtrace-patch-all=1\n")
        py_file = self.testdir.makepyfile(
            """
            import ddtrace

            def test_patched_all():
                assert ddtrace._monkey._PATCHED_MODULES
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run(file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 0

    def test_disabled(self):
        """Test without --ddtrace."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            def test_no_trace(ddspan):
                assert ddspan is None
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run(file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 0

    def test_ini(self):
        """Test ini config."""
        self.testdir.makefile(".ini", pytest="[pytest]\nddtrace=1\n")
        py_file = self.testdir.makepyfile(
            """
            import pytest

            def test_ini(ddspan):
                assert ddspan is not None
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run(file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 3

    def test_pytest_command(self):
        """Test that the pytest run command is stored on a test span."""
        py_file = self.testdir.makepyfile(
            """
            def test_ok():
                assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()
        test_span = spans[0]
        if PY2:
            assert test_span.get_tag("test.command") == "pytest"
        else:
            assert test_span.get_tag("test.command") == "pytest --ddtrace {}".format(file_name)

    def test_parameterize_case(self):
        """Test parametrize case with simple objects."""
        py_file = self.testdir.makepyfile(
            """
            import pytest


            @pytest.mark.parametrize('item', [1, 2, 3, 4, pytest.param([1, 2, 3], marks=pytest.mark.skip)])
            class Test1(object):
                def test_1(self, item):
                    assert item in {1, 2, 3}
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=3, failed=1, skipped=1)
        spans = self.pop_spans()

        expected_params = [1, 2, 3, 4, [1, 2, 3]]
        assert len(spans) == 7
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        for i in range(len(expected_params)):
            assert json.loads(test_spans[i].get_tag(test.PARAMETERS)) == {
                "arguments": {"item": str(expected_params[i])},
                "metadata": {},
            }

    def test_parameterize_case_complex_objects(self):
        """Test parametrize case with complex objects."""
        py_file = self.testdir.makepyfile(
            """
            from mock import MagicMock
            import pytest

            class A:
                def __init__(self, name, value):
                    self.name = name
                    self.value = value

            def item_param():
                return 42

            circular_reference = A("circular_reference", A("child", None))
            circular_reference.value.value = circular_reference

            @pytest.mark.parametrize(
            'item',
            [
                pytest.param(A("test_name", "value"), marks=pytest.mark.skip),
                pytest.param(A("test_name", A("inner_name", "value")), marks=pytest.mark.skip),
                pytest.param(item_param, marks=pytest.mark.skip),
                pytest.param({"a": A("test_name", "value"), "b": [1, 2, 3]}, marks=pytest.mark.skip),
                pytest.param(MagicMock(value=MagicMock()), marks=pytest.mark.skip),
                pytest.param(circular_reference, marks=pytest.mark.skip),
                pytest.param({("x", "y"): 12345}, marks=pytest.mark.skip)
            ]
            )
            class Test1(object):
                def test_1(self, item):
                    assert item in {1, 2, 3}
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=7)
        spans = self.pop_spans()

        # Since object will have arbitrary addresses, only need to ensure that
        # the params string contains most of the string representation of the object.
        expected_params_contains = [
            "test_parameterize_case_complex_objects.A",
            "test_parameterize_case_complex_objects.A",
            "<function item_param>",
            "'a': <test_parameterize_case_complex_objects.A",
            "<MagicMock id=",
            "test_parameterize_case_complex_objects.A",
            "{('x', 'y'): 12345}",
        ]
        assert len(spans) == 9
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        for i in range(len(expected_params_contains)):
            assert expected_params_contains[i] in test_spans[i].get_tag(test.PARAMETERS)

    def test_parameterize_case_encoding_error(self):
        """Test parametrize case with complex objects that cannot be JSON encoded."""
        py_file = self.testdir.makepyfile(
            """
            from mock import MagicMock
            import pytest

            class A:
                def __repr__(self):
                    raise Exception("Cannot __repr__")

            @pytest.mark.parametrize('item',[A()])
            class Test1(object):
                def test_1(self, item):
                    assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]
        assert json.loads(test_span.get_tag(test.PARAMETERS)) == {
            "arguments": {"item": "Could not encode"},
            "metadata": {},
        }

    def test_skip(self):
        """Test skip case."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.skip(reason="decorator")
            def test_decorator():
                pass

            def test_body():
                pytest.skip("body")
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=2)
        spans = self.pop_spans()

        assert len(spans) == 4
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert test_spans[0].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[0].get_tag(test.SKIP_REASON) == "decorator"
        assert test_spans[1].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[1].get_tag(test.SKIP_REASON) == "body"
        assert test_spans[0].get_tag("component") == "pytest"
        assert test_spans[1].get_tag("component") == "pytest"

    def test_skip_module_with_xfail_cases(self):
        """Test Xfail test cases for a module that is skipped entirely, which should be treated as skip tests."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.skip(reason="reason")

            @pytest.mark.xfail(reason="XFail Case")
            def test_xfail():
                pass

            @pytest.mark.xfail(condition=False, reason="XFail Case")
            def test_xfail_conditional():
                pass
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=2)
        spans = self.pop_spans()

        assert len(spans) == 4
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert test_spans[0].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[0].get_tag(test.SKIP_REASON) == "reason"
        assert test_spans[1].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[1].get_tag(test.SKIP_REASON) == "reason"
        assert test_spans[0].get_tag("component") == "pytest"
        assert test_spans[1].get_tag("component") == "pytest"

    def test_skipif_module(self):
        """Test XFail test cases for a module that is skipped entirely with the skipif marker."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            pytestmark = pytest.mark.skipif(True, reason="reason")

            @pytest.mark.xfail(reason="XFail")
            def test_xfail():
                pass

            @pytest.mark.xfail(condition=False, reason="XFail Case")
            def test_xfail_conditional():
                pass
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=2)
        spans = self.pop_spans()

        assert len(spans) == 4
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert test_spans[0].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[0].get_tag(test.SKIP_REASON) == "reason"
        assert test_spans[1].get_tag(test.STATUS) == test.Status.SKIP.value
        assert test_spans[1].get_tag(test.SKIP_REASON) == "reason"
        assert test_spans[0].get_tag("component") == "pytest"
        assert test_spans[1].get_tag("component") == "pytest"

    def test_xfail_fails(self):
        """Test xfail (expected failure) which fails, should be marked as pass."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.xfail(reason="test should fail")
            def test_should_fail():
                assert 0

            @pytest.mark.xfail(condition=True, reason="test should xfail")
            def test_xfail_conditional():
                assert 0
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        # pytest records xfail as skipped
        rec.assertoutcome(skipped=2)
        spans = self.pop_spans()

        assert len(spans) == 4
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert test_spans[0].get_tag(test.STATUS) == test.Status.PASS.value
        assert test_spans[0].get_tag(test.RESULT) == test.Status.XFAIL.value
        assert test_spans[0].get_tag(XFAIL_REASON) == "test should fail"
        assert test_spans[1].get_tag(test.STATUS) == test.Status.PASS.value
        assert test_spans[1].get_tag(test.RESULT) == test.Status.XFAIL.value
        assert test_spans[1].get_tag(XFAIL_REASON) == "test should xfail"
        assert test_spans[0].get_tag("component") == "pytest"
        assert test_spans[1].get_tag("component") == "pytest"

    def test_xfail_runxfail_fails(self):
        """Test xfail with --runxfail flags should not crash when failing."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.xfail(reason='should fail')
            def test_should_fail():
                assert 0

        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", "--runxfail", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        assert spans[0].get_tag(test.STATUS) == test.Status.FAIL.value

    def test_xfail_runxfail_passes(self):
        """Test xfail with --runxfail flags should not crash when passing."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.xfail(reason='should fail')
            def test_should_pass():
                assert 1

        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", "--runxfail", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        assert spans[0].get_tag(test.STATUS) == test.Status.PASS.value

    def test_xpass_not_strict(self):
        """Test xpass (unexpected passing) with strict=False, should be marked as pass."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.xfail(reason="test should fail")
            def test_should_fail_but_passes():
                pass

            @pytest.mark.xfail(condition=True, reason="test should not xfail")
            def test_should_not_fail():
                pass
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=2)
        spans = self.pop_spans()

        assert len(spans) == 4
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert test_spans[0].get_tag(test.STATUS) == test.Status.PASS.value
        assert test_spans[0].get_tag(test.RESULT) == test.Status.XPASS.value
        assert test_spans[0].get_tag(XFAIL_REASON) == "test should fail"
        assert test_spans[1].get_tag(test.STATUS) == test.Status.PASS.value
        assert test_spans[1].get_tag(test.RESULT) == test.Status.XPASS.value
        assert test_spans[1].get_tag(XFAIL_REASON) == "test should not xfail"
        assert test_spans[0].get_tag("component") == "pytest"
        assert test_spans[1].get_tag("component") == "pytest"

    def test_xpass_strict(self):
        """Test xpass (unexpected passing) with strict=True, should be marked as fail."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.xfail(reason="test should fail", strict=True)
            def test_should_fail():
                pass
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(failed=1)
        spans = self.pop_spans()

        assert len(spans) == 3
        span = [span for span in spans if span.get_tag("type") == "test"][0]
        assert span.get_tag(test.STATUS) == test.Status.FAIL.value
        assert span.get_tag(test.RESULT) == test.Status.XPASS.value
        # Note: XFail (strict=True) does not mark the reason with result.wasxfail but into result.longrepr,
        # however it provides the entire traceback/error into longrepr.
        assert "test should fail" in span.get_tag(XFAIL_REASON)

    def test_tags(self):
        """Test ddspan tags."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.dd_tags(mark="dd_tags")
            def test_fixture(ddspan):
                assert ddspan is not None
                ddspan.set_tag("world", "hello")
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]
        assert test_span.get_tag("world") == "hello"
        assert test_span.get_tag("mark") == "dd_tags"
        assert test_span.get_tag(test.STATUS) == test.Status.PASS.value
        assert test_span.get_tag("component") == "pytest"

    def test_service_name_repository_name(self):
        """Test span's service name is set to repository name."""
        self.monkeypatch.setenv("APPVEYOR", "true")
        self.monkeypatch.setenv("APPVEYOR_REPO_PROVIDER", "github")
        self.monkeypatch.setenv("APPVEYOR_REPO_NAME", "test-repository-name")
        py_file = self.testdir.makepyfile(
            """
            import os

            def test_service(ddspan):
                assert 'test-repository-name' == ddspan.service
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.subprocess_run("--ddtrace", file_name)
        rec.assert_outcomes(passed=1)

    def test_default_service_name(self):
        """Test default service name if no repository name found."""
        providers = [provider for (provider, extract) in ci.PROVIDERS]
        for provider in providers:
            self.monkeypatch.delenv(provider, raising=False)
        py_file = self.testdir.makepyfile(
            """
            def test_service(ddspan):
                assert ddspan.service == "pytest"
                assert ddspan.name == "pytest.test"
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.subprocess_run("--ddtrace", file_name)
        rec.assert_outcomes(passed=1)

    def test_dd_service_name(self):
        """Test dd service name."""
        self.monkeypatch.setenv("DD_SERVICE", "mysvc")
        if "DD_PYTEST_SERVICE" in os.environ:
            self.monkeypatch.delenv("DD_PYTEST_SERVICE")

        py_file = self.testdir.makepyfile(
            """
            import os

            def test_service(ddspan):
                assert 'mysvc' == os.getenv('DD_SERVICE')
                assert os.getenv('DD_PYTEST_SERVICE') is None
                assert 'mysvc' == ddspan.service
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.subprocess_run("--ddtrace", file_name)
        assert 0 == rec.ret

    def test_dd_pytest_service_name(self):
        """Test integration service name."""
        self.monkeypatch.setenv("DD_SERVICE", "mysvc")
        self.monkeypatch.setenv("DD_PYTEST_SERVICE", "pymysvc")
        self.monkeypatch.setenv("DD_PYTEST_OPERATION_NAME", "mytest")

        py_file = self.testdir.makepyfile(
            """
            import os

            def test_service(ddspan):
                assert 'mysvc' == os.getenv('DD_SERVICE')
                assert 'pymysvc' == os.getenv('DD_PYTEST_SERVICE')
                assert 'pymysvc' == ddspan.service
                assert 'mytest' == ddspan.name
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.subprocess_run("--ddtrace", file_name)
        assert 0 == rec.ret

    def test_dd_origin_tag_propagated_to_every_span(self):
        """Test that every span in generated trace has the dd_origin tag."""
        py_file = self.testdir.makepyfile(
            """
            import pytest
            import ddtrace
            from ddtrace import Pin

            def test_service(ddtracer):
                with ddtracer.trace("SPAN2") as span2:
                    with ddtracer.trace("SPAN3") as span3:
                        with ddtracer.trace("SPAN4") as span4:
                            assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()
        # Check if spans tagged with dd_origin after encoding and decoding as the tagging occurs at encode time
        encoder = self.tracer.encoder
        encoder.put(spans)
        trace = encoder.encode()
        (decoded_trace,) = self.tracer.encoder._decode(trace)
        assert len(decoded_trace) == 6
        for span in decoded_trace:
            assert span[b"meta"][b"_dd.origin"] == b"ciapp-test"

        ci_agentless_encoder = CIVisibilityEncoderV01(0, 0)
        ci_agentless_encoder.put(spans)
        event_payload = ci_agentless_encoder.encode()
        decoded_event_payload = self.tracer.encoder._decode(event_payload)
        assert len(decoded_event_payload[b"events"]) == 6
        for event in decoded_event_payload[b"events"]:
            assert event[b"content"][b"meta"][b"_dd.origin"] == b"ciapp-test"
        pass

    def test_pytest_doctest_module(self):
        """Test that pytest with doctest works as expected."""
        py_file = self.testdir.makepyfile(
            """
        '''
        This module supplies one function foo(). For example,
        >>> foo()
        42
        '''

        def foo():
            '''Returns the answer to life, the universe, and everything.
            >>> foo()
            42
            '''
            return 42

        def test_foo():
            assert foo() == 42
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", "--doctest-modules", file_name)
        rec.assertoutcome(passed=3)
        spans = self.pop_spans()

        assert len(spans) == 6
        non_session_spans = [span for span in spans if span.get_tag("type") != "test_session_end"]
        for span in non_session_spans:
            assert span.get_tag(test.SUITE) == file_name
        test_session_span = spans[5]
        if PY2:
            assert test_session_span.get_tag("test.command") == "pytest"
        else:
            assert test_session_span.get_tag("test.command") == (
                "pytest --ddtrace --doctest-modules " "test_pytest_doctest_module.py"
            )

    def test_pytest_sets_sample_priority(self):
        """Test sample priority tags."""
        py_file = self.testdir.makepyfile(
            """
            def test_sample_priority():
                assert True is True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()

        assert len(spans) == 3
        assert spans[0].get_metric(SAMPLING_PRIORITY_KEY) == 1

    def test_pytest_exception(self):
        """Test that pytest sets exception information correctly."""
        py_file = self.testdir.makepyfile(
            """
        def test_will_fail():
            assert 2 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]
        assert test_span.get_tag(test.STATUS) == test.Status.FAIL.value
        assert test_span.get_tag("error.type").endswith("AssertionError") is True
        assert test_span.get_tag(ERROR_MSG) == "assert 2 == 1"
        assert test_span.get_tag("error.stack") is not None
        assert test_span.get_tag("component") == "pytest"

    def test_pytest_tests_with_internal_exceptions_get_test_status(self):
        """Test that pytest sets a fail test status if it has an internal exception."""
        py_file = self.testdir.makepyfile(
            """
        import pytest

        # This is bad usage and results in a pytest internal exception
        @pytest.mark.filterwarnings("ignore::pytest.ExceptionThatDoesNotExist")
        def test_will_fail_internally():
            assert 2 == 2
        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]
        assert test_span.get_tag(test.STATUS) == test.Status.FAIL.value
        assert test_span.get_tag("error.type") is None
        assert test_span.get_tag("component") == "pytest"

    def test_pytest_broken_setup_will_be_reported_as_error(self):
        """Test that pytest sets a fail test status if the setup fails."""
        py_file = self.testdir.makepyfile(
            """
        import pytest

        @pytest.fixture
        def my_fixture():
            raise Exception('will fail in setup')
            yield

        def test_will_fail_in_setup(my_fixture):
            assert 1 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]

        assert test_span.get_tag(test.STATUS) == test.Status.FAIL.value
        assert test_span.get_tag("error.type").endswith("Exception") is True
        assert test_span.get_tag(ERROR_MSG) == "will fail in setup"
        assert test_span.get_tag("error.stack") is not None
        assert test_span.get_tag("component") == "pytest"

    def test_pytest_broken_teardown_will_be_reported_as_error(self):
        """Test that pytest sets a fail test status if the teardown fails."""
        py_file = self.testdir.makepyfile(
            """
        import pytest

        @pytest.fixture
        def my_fixture():
            yield
            raise Exception('will fail in teardown')

        def test_will_fail_in_teardown(my_fixture):
            assert 1 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]

        assert test_span.get_tag(test.STATUS) == test.Status.FAIL.value
        assert test_span.get_tag("error.type").endswith("Exception") is True
        assert test_span.get_tag(ERROR_MSG) == "will fail in teardown"
        assert test_span.get_tag("error.stack") is not None
        assert test_span.get_tag("component") == "pytest"

    def test_pytest_will_report_its_version(self):
        py_file = self.testdir.makepyfile(
            """
        import pytest

        def test_will_work():
            assert 1 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        self.inline_run("--ddtrace", file_name)
        spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[2]

        assert test_span.get_tag(test.FRAMEWORK_VERSION) == pytest.__version__

    def test_pytest_will_report_codeowners(self):
        file_names = []
        py_team_a_file = self.testdir.makepyfile(
            test_team_a="""
        import pytest

        def test_team_a():
            assert 1 == 1
        """
        )
        file_names.append(os.path.basename(py_team_a_file.strpath))
        py_team_b_file = self.testdir.makepyfile(
            test_team_b="""
        import pytest

        def test_team_b():
            assert 1 == 1
        """
        )
        file_names.append(os.path.basename(py_team_b_file.strpath))
        codeowners = "* @default-team\n{0} @team-b @backup-b".format(os.path.basename(py_team_b_file.strpath))
        self.testdir.makefile("", CODEOWNERS=codeowners)

        self.inline_run("--ddtrace", *file_names)
        spans = self.pop_spans()
        assert len(spans) == 5
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        assert json.loads(test_spans[0].get_tag(test.CODEOWNERS)) == ["@default-team"], test_spans[0]
        assert json.loads(test_spans[1].get_tag(test.CODEOWNERS)) == ["@team-b", "@backup-b"], test_spans[1]

    def test_pytest_session(self):
        """Test that running pytest will generate a test session span."""
        self.inline_run("--ddtrace")
        spans = self.pop_spans()
        assert len(spans) == 1
        assert spans[0].get_tag("type") == "test_session_end"
        assert spans[0].get_tag("test_session_id") == str(spans[0].span_id)
        if PY2:
            assert spans[0].get_tag("test.command") == "pytest"
        else:
            assert spans[0].get_tag("test.command") == "pytest --ddtrace"

    def test_pytest_test_class_hierarchy_is_added_to_test_span(self):
        """Test that given a test class, the test span will include the hierarchy of test class(es) as a tag."""
        py_file = self.testdir.makepyfile(
            """
            class TestNestedOuter:
                class TestNestedInner:
                    def test_ok(self):
                        assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()
        assert len(spans) == 3
        test_span = spans[0]
        assert test_span.get_tag("test.class_hierarchy") == "TestNestedOuter.TestNestedInner"

    def test_pytest_suite(self):
        """Test that running pytest on a test file will generate a test suite span."""
        py_file = self.testdir.makepyfile(
            """
            def test_ok():
                assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1)
        spans = self.pop_spans()
        test_suite_span = spans[2]
        test_session_span = spans[1]
        assert test_suite_span.get_tag("type") == "test_suite_end"
        assert test_suite_span.get_tag("test_session_id") == str(test_session_span.span_id)
        assert test_suite_span.get_tag("test_module_id") is None
        assert test_suite_span.get_tag("test_suite_id") == str(test_suite_span.span_id)
        assert test_suite_span.get_tag("test.status") == "pass"
        assert test_session_span.get_tag("test.status") == "pass"
        if PY2:
            assert test_suite_span.get_tag("test.command") == "pytest"
        else:
            assert test_suite_span.get_tag("test.command") == "pytest --ddtrace {}".format(file_name)
        assert test_suite_span.get_tag("test.suite") == str(file_name)

    def test_pytest_suites(self):
        """
        Test that running pytest on two files with 1 test each will generate
         1 test session span, 2 test suite spans, and 2 test spans.
        """
        file_names = []
        file_a = self.testdir.makepyfile(
            test_a="""
        def test_ok():
            assert True
        """
        )
        file_names.append(os.path.basename(file_a.strpath))
        file_b = self.testdir.makepyfile(
            test_b="""
        def test_not_ok():
            assert 0
        """
        )
        file_names.append(os.path.basename(file_b.strpath))
        self.inline_run("--ddtrace")
        spans = self.pop_spans()

        assert len(spans) == 5
        test_session_span = spans[2]
        assert test_session_span.name == "pytest.test_session"
        assert test_session_span.parent_id is None
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        for test_span in test_spans:
            assert test_span.name == "pytest.test"
            assert test_span.parent_id is None
        test_suite_spans = [span for span in spans if span.get_tag("type") == "test_suite_end"]
        for test_suite_span in test_suite_spans:
            assert test_suite_span.name == "pytest.test_suite"
            assert test_suite_span.parent_id == test_session_span.span_id

    def test_pytest_test_class_does_not_prematurely_end_test_suite(self):
        """Test that given a test class, the test suite span will not end prematurely."""
        self.testdir.makepyfile(
            test_a="""
                def test_outside_class_before():
                    assert True
                class TestClass:
                    def test_ok(self):
                        assert True
                def test_outside_class_after():
                    assert True
                """
        )
        rec = self.inline_run("--ddtrace")
        rec.assertoutcome(passed=3)
        spans = self.pop_spans()
        assert len(spans) == 5
        test_span_a_inside_class = spans[1]
        test_span_a_outside_after_class = spans[2]
        test_suite_a_span = spans[4]
        assert test_span_a_inside_class.get_tag("test.class_hierarchy") == "TestClass"
        assert test_suite_a_span.start_ns + test_suite_a_span.duration_ns >= test_span_a_outside_after_class.start_ns

    def test_pytest_suites_one_fails_propagates(self):
        """Test that if any tests fail, the status propagates upwards."""
        file_names = []
        file_a = self.testdir.makepyfile(
            test_a="""
                def test_ok():
                    assert True
                """
        )
        file_names.append(os.path.basename(file_a.strpath))
        file_b = self.testdir.makepyfile(
            test_b="""
                def test_not_ok():
                    assert 0
                """
        )
        file_names.append(os.path.basename(file_b.strpath))
        self.inline_run("--ddtrace")
        spans = self.pop_spans()
        test_session_span = spans[2]
        test_a_suite_span = spans[3]
        test_b_suite_span = spans[4]
        assert test_session_span.get_tag("test.status") == "fail"
        assert test_a_suite_span.get_tag("test.status") == "pass"
        assert test_b_suite_span.get_tag("test.status") == "fail"

    def test_pytest_suites_one_skip_does_not_propagate(self):
        """Test that if not all tests skip, the status does not propagate upwards."""
        file_names = []
        file_a = self.testdir.makepyfile(
            test_a="""
                def test_ok():
                    assert True
                """
        )
        file_names.append(os.path.basename(file_a.strpath))
        file_b = self.testdir.makepyfile(
            test_b="""
                import pytest
                @pytest.mark.skip(reason="Because")
                def test_not_ok():
                    assert 0
                """
        )
        file_names.append(os.path.basename(file_b.strpath))
        self.inline_run("--ddtrace")
        spans = self.pop_spans()
        test_session_span = spans[2]
        test_a_suite_span = spans[3]
        test_b_suite_span = spans[4]
        assert test_session_span.get_tag("test.status") == "pass"
        assert test_a_suite_span.get_tag("test.status") == "pass"
        assert test_b_suite_span.get_tag("test.status") == "skip"

    def test_pytest_all_tests_pass_status_propagates(self):
        """Test that if all tests pass, the status propagates upwards."""
        py_file = self.testdir.makepyfile(
            """
            def test_ok():
                assert True

            def test_ok_2():
                assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=2)
        spans = self.pop_spans()
        for span in spans:
            assert span.get_tag("test.status") == "pass"

    def test_pytest_status_fail_propagates(self):
        """Test that if any tests fail, that status propagates upwards.
        In other words, any test failure will cause the test suite to be marked as fail, as well as module, and session.
        """
        py_file = self.testdir.makepyfile(
            """
            def test_ok():
                assert True

            def test_not_ok():
                assert 0
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(passed=1, failed=1)
        spans = self.pop_spans()
        test_span_ok = spans[0]
        test_span_not_ok = spans[1]
        test_suite_span = spans[3]
        test_session_span = spans[2]
        assert test_span_ok.get_tag("test.status") == "pass"
        assert test_span_not_ok.get_tag("test.status") == "fail"
        assert test_suite_span.get_tag("test.status") == "fail"
        assert test_session_span.get_tag("test.status") == "fail"

    def test_pytest_all_tests_skipped_propagates(self):
        """Test that if all tests are skipped, that status propagates upwards.
        In other words, all test skips will cause the test suite to be marked as skipped,
        and the same logic for module and session.
        """
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.skip(reason="Because")
            def test_not_ok_but_skipped():
                assert 0

            @pytest.mark.skip(reason="Because")
            def test_also_not_ok_but_skipped():
                assert 0
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=2)
        spans = self.pop_spans()
        for span in spans:
            assert span.get_tag("test.status") == "skip"

    def test_pytest_not_all_tests_skipped_does_not_propagate(self):
        """Test that if not all tests are skipped, that status does not propagate upwards."""
        py_file = self.testdir.makepyfile(
            """
            import pytest

            @pytest.mark.skip(reason="Because")
            def test_not_ok_but_skipped():
                assert 0

            def test_ok():
                assert True
        """
        )
        file_name = os.path.basename(py_file.strpath)
        rec = self.inline_run("--ddtrace", file_name)
        rec.assertoutcome(skipped=1, passed=1)
        spans = self.pop_spans()
        test_span_skipped = spans[0]
        test_span_ok = spans[1]
        test_suite_span = spans[3]
        test_session_span = spans[2]
        assert test_span_skipped.get_tag("test.status") == "skip"
        assert test_span_ok.get_tag("test.status") == "pass"
        assert test_suite_span.get_tag("test.status") == "pass"
        assert test_session_span.get_tag("test.status") == "pass"

    def test_pytest_module(self):
        """Test that running pytest on a test package will generate a test module span."""
        package_a_dir = self.testdir.mkpydir("test_package_a")
        os.chdir(str(package_a_dir))
        with open("test_a.py", "w+") as fd:
            fd.write(
                """def test_ok():
                assert True"""
            )
        self.testdir.chdir()
        self.inline_run("--ddtrace")
        spans = self.pop_spans()
        for span in spans:
            assert span.get_tag("test.status") == "pass"
        assert len(spans) == 4
        test_module_span = spans[2]
        test_session_span = spans[1]
        assert test_module_span.get_tag("type") == "test_module_end"
        assert test_module_span.get_tag("test_session_id") == str(test_session_span.span_id)
        assert test_module_span.get_tag("test_module_id") == str(test_module_span.span_id)
        if PY2:
            assert test_module_span.get_tag("test.command") == "pytest"
        else:
            assert test_module_span.get_tag("test.command") == "pytest --ddtrace"
        assert test_module_span.get_tag("test.module") == str(package_a_dir).split("/")[-1]
        assert test_module_span.get_tag("test.module_path") == str(package_a_dir).split("/")[-1]

    def test_pytest_modules(self):
        """
        Test that running pytest on two packages with 1 test each will generate
         1 test session span, 2 test module spans, 2 test suite spans, and 2 test spans.
        """
        package_a_dir = self.testdir.mkpydir("test_package_a")
        os.chdir(str(package_a_dir))
        with open("test_a.py", "w+") as fd:
            fd.write(
                """def test_ok():
                assert True"""
            )
        package_b_dir = self.testdir.mkpydir("test_package_b")
        os.chdir(str(package_b_dir))
        with open("test_b.py", "w+") as fd:
            fd.write(
                """def test_not_ok():
                assert 0"""
            )
        self.testdir.chdir()
        self.inline_run("--ddtrace")
        spans = self.pop_spans()

        assert len(spans) == 7
        test_session_span = spans[2]
        assert test_session_span.name == "pytest.test_session"
        assert test_session_span.get_tag("test.status") == "fail"
        test_module_spans = [span for span in spans if span.get_tag("type") == "test_module_end"]
        for span in test_module_spans:
            assert span.name == "pytest.test_module"
            assert span.parent_id == test_session_span.span_id
        test_suite_spans = [span for span in spans if span.get_tag("type") == "test_suite_end"]
        for i in range(len(test_suite_spans)):
            assert test_suite_spans[i].name == "pytest.test_suite"
            assert test_suite_spans[i].parent_id == test_module_spans[i].span_id
        test_spans = [span for span in spans if span.get_tag("type") == "test"]
        for i in range(len(test_spans)):
            assert test_spans[i].name == "pytest.test"
            assert test_spans[i].parent_id is None
            assert test_spans[i].get_tag("test_module_id") == str(test_module_spans[i].span_id)

    def test_pytest_test_class_does_not_prematurely_end_test_module(self):
        """Test that given a test class, the test module span will not end prematurely."""
        package_a_dir = self.testdir.mkpydir("test_package_a")
        os.chdir(str(package_a_dir))
        with open("test_a.py", "w+") as fd:
            fd.write(
                "def test_ok():\n\tassert True\n"
                "class TestClassOuter:\n"
                "\tclass TestClassInner:\n"
                "\t\tdef test_class_inner(self):\n\t\t\tassert True\n"
                "\tdef test_class_outer(self):\n\t\tassert True\n"
                "def test_after_class():\n\tassert True"
            )
        self.testdir.chdir()
        rec = self.inline_run("--ddtrace")
        rec.assertoutcome(passed=4)
        spans = self.pop_spans()
        assert len(spans) == 7
        test_span_outside_after_class = spans[3]
        test_module_span = spans[5]
        assert test_module_span.start_ns + test_module_span.duration_ns >= test_span_outside_after_class.start_ns

    def test_pytest_packages_skip_one(self):
        """
        Test that running pytest on two packages with 1 test each, but skipping one package will generate
         1 test session span, 2 test module spans, 2 test suite spans, and 2 test spans.
        """
        package_a_dir = self.testdir.mkpydir("test_package_a")
        os.chdir(str(package_a_dir))
        with open("test_a.py", "w+") as fd:
            fd.write(
                """def test_not_ok():
                assert 0"""
            )
        package_b_dir = self.testdir.mkpydir("test_package_b")
        os.chdir(str(package_b_dir))
        with open("test_b.py", "w+") as fd:
            fd.write(
                """def test_ok():
                assert True"""
            )
        self.testdir.chdir()
        self.inline_run("--ignore=test_package_a", "--ddtrace")
        spans = self.pop_spans()
        assert len(spans) == 4
        test_session_span = spans[1]
        assert test_session_span.name == "pytest.test_session"
        assert test_session_span.get_tag("test.status") == "pass"
        test_module_span = spans[2]
        assert test_module_span.name == "pytest.test_module"
        assert test_module_span.parent_id == test_session_span.span_id
        assert test_module_span.get_tag("test.status") == "pass"
        test_suite_span = spans[3]
        assert test_suite_span.name == "pytest.test_suite"
        assert test_suite_span.parent_id == test_module_span.span_id
        assert test_suite_span.get_tag("test_module_id") == str(test_module_span.span_id)
        assert test_suite_span.get_tag("test.status") == "pass"
        test_span = spans[0]
        assert test_span.name == "pytest.test"
        assert test_span.parent_id is None
        assert test_span.get_tag("test_module_id") == str(test_module_span.span_id)
        assert test_span.get_tag("test.status") == "pass"

    def test_pytest_module_path(self):
        """
        Test that running pytest on two nested packages with 1 test each will generate
         1 test session span, 2 test module spans, 2 test suite spans, and 2 test spans,
         with the module spans including correct module paths.
        """
        package_outer_dir = self.testdir.mkpydir("test_outer_package")
        os.chdir(str(package_outer_dir))
        with open("test_outer_abc.py", "w+") as fd:
            fd.write(
                """def test_ok():
                assert True"""
            )
        os.mkdir("test_inner_package")
        os.chdir("test_inner_package")
        with open("__init__.py", "w+"):
            pass
        with open("test_inner_abc.py", "w+") as fd:
            fd.write(
                """def test_ok():
                assert True"""
            )
        self.testdir.chdir()
        self.inline_run("--ddtrace")
        spans = self.pop_spans()

        assert len(spans) == 7
        test_module_spans = [span for span in spans if span.get_tag("type") == "test_module_end"]
        assert test_module_spans[0].get_tag("test.module") == "test_outer_package"
        assert test_module_spans[0].get_tag("test.module_path") == "test_outer_package"
        assert test_module_spans[1].get_tag("test.module") == "test_inner_package"
        assert test_module_spans[1].get_tag("test.module_path") == "test_outer_package/test_inner_package"

    @pytest.mark.skipif(compat.PY2, reason="ddtrace does not support coverage on Python 2")
    def test_pytest_will_report_coverage(self):
        self.testdir.makepyfile(
            test_module="""
        def lib_fn():
            return True
        """
        )
        py_cov_file = self.testdir.makepyfile(
            test_cov="""
        import pytest
        from test_module import lib_fn

        def test_cov():
            assert lib_fn()
        """
        )

        with override_global_config({"_ci_visibility_code_coverage_enabled": True}):
            self.inline_run("--ddtrace", os.path.basename(py_cov_file.strpath))
        spans = self.pop_spans()

        assert COVERAGE_TAG_NAME in spans[0].get_tags()
        tag_data = json.loads(spans[0].get_tag(COVERAGE_TAG_NAME))
        files = sorted(tag_data["files"], key=lambda x: x["filename"])
        assert files[0]["filename"] == "test_cov.py"
        assert files[1]["filename"] == "test_module.py"
        assert files[0]["segments"][0] == [5, 0, 5, 0, -1]
        assert files[1]["segments"][0] == [2, 0, 2, 0, -1]

    def test_pytest_will_report_git_metadata(self):
        py_file = self.testdir.makepyfile(
            """
        import pytest

        def test_will_work():
            assert 1 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        with mock.patch("ddtrace.internal.ci_visibility.recorder._get_git_repo") as ggr:
            ggr.return_value = self.git_repo
            self.inline_run("--ddtrace", file_name)
            spans = self.pop_spans()

        assert len(spans) == 3
        test_span = spans[0]

        assert test_span.get_tag(git.COMMIT_MESSAGE) == "this is a commit msg"
        assert test_span.get_tag(git.COMMIT_AUTHOR_DATE) == "2021-01-19T09:24:53-0400"
        assert test_span.get_tag(git.COMMIT_AUTHOR_NAME) == "John Doe"
        assert test_span.get_tag(git.COMMIT_AUTHOR_EMAIL) == "john@doe.com"
        assert test_span.get_tag(git.COMMIT_COMMITTER_DATE) == "2021-01-20T04:37:21-0400"
        assert test_span.get_tag(git.COMMIT_COMMITTER_NAME) == "Jane Doe"
        assert test_span.get_tag(git.COMMIT_COMMITTER_EMAIL) == "jane@doe.com"
        assert test_span.get_tag(git.BRANCH)
        assert test_span.get_tag(git.COMMIT_SHA)
        assert test_span.get_tag(git.REPOSITORY_URL)

    def test_pytest_skipped_by_itr(self):
        py_file = self.testdir.makepyfile(
            """
        def test_will_work():
            assert 1 == 1

        def test_not_ok():
            assert 0 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        with mock.patch(
            "ddtrace.internal.ci_visibility.recorder.CIVisibility.test_skipping_enabled",
            return_value=[
                True,
            ],
        ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"), mock.patch(
            "ddtrace.internal.ci_visibility.recorder.CIVisibility._get_tests_to_skip",
            return_value=[
                "test_will_work",
            ],
        ), mock.patch(
            "pytest.skip"
        ) as pytest_skip:
            self.inline_run("--ddtrace", file_name)
            spans = self.pop_spans()

        pytest_skip.assert_called_once_with("Skipped by Datadog Intelligent Test Runner")

        assert len(spans) == 3
        test_test_span = spans[0]
        assert test_test_span.name == "pytest.test"
        assert test_test_span.get_tag("test.status") == "fail"
        assert test_test_span.get_tag("test.name") == "test_not_ok"

    def test_pytest_not_skipped_by_itr_empty_tests_to_skip(self):
        py_file = self.testdir.makepyfile(
            """
        def test_will_work():
            assert 1 == 1

        def test_not_ok():
            assert 0 == 1
        """
        )
        file_name = os.path.basename(py_file.strpath)
        with mock.patch(
            "ddtrace.internal.ci_visibility.recorder.CIVisibility.test_skipping_enabled",
            return_value=[
                True,
            ],
        ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"), mock.patch(
            "ddtrace.internal.ci_visibility.recorder.CIVisibility._get_tests_to_skip",
            return_value=[],
        ), mock.patch(
            "pytest.skip"
        ) as pytest_skip:
            self.inline_run("--ddtrace", file_name)
            spans = self.pop_spans()

        pytest_skip.assert_not_called()

        assert len(spans) == 4
        test_passed_test_span = spans[0]
        assert test_passed_test_span.name == "pytest.test"
        assert test_passed_test_span.get_tag("test.status") == "pass"
        assert test_passed_test_span.get_tag("test.name") == "test_will_work"

        test_failed_test_span = spans[1]
        assert test_failed_test_span.name == "pytest.test"
        assert test_failed_test_span.get_tag("test.status") == "fail"
        assert test_failed_test_span.get_tag("test.name") == "test_not_ok"

import contextlib
import json
import os
import time

import mock
import pytest

import ddtrace
from ddtrace.constants import AUTO_KEEP
from ddtrace.ext import ci
from ddtrace.internal.ci_visibility import CIVisibility
from ddtrace.internal.ci_visibility.constants import REQUESTS_MODE
from ddtrace.internal.ci_visibility.filters import TraceCiVisibilityFilter
from ddtrace.internal.ci_visibility.git_client import CIVisibilityGitClient
from ddtrace.internal.ci_visibility.git_client import CIVisibilityGitClientSerializerV1
from ddtrace.internal.ci_visibility.recorder import _extract_repository_name_from_url
from ddtrace.internal.utils.http import Response
from ddtrace.span import Span
from tests.utils import DummyCIVisibilityWriter
from tests.utils import DummyTracer
from tests.utils import override_env
from tests.utils import override_global_config


TEST_SHA = "b3672ea5cbc584124728c48a443825d2940e0ddd"


def test_filters_test_spans():
    trace_filter = TraceCiVisibilityFilter(tags={"hello": "world"}, service="test-service")
    root_test_span = Span(name="span1", span_type="test")
    root_test_span._local_root = root_test_span
    # Root span in trace is a test
    trace = [root_test_span]
    assert trace_filter.process_trace(trace) == trace
    assert root_test_span.service == "test-service"
    assert root_test_span.get_tag(ci.LIBRARY_VERSION) == ddtrace.__version__
    assert root_test_span.get_tag("hello") == "world"
    assert root_test_span.context.dd_origin == ci.CI_APP_TEST_ORIGIN
    assert root_test_span.context.sampling_priority == AUTO_KEEP


def test_filters_non_test_spans():
    trace_filter = TraceCiVisibilityFilter(tags={"hello": "world"}, service="test-service")
    root_span = Span(name="span1")
    root_span._local_root = root_span
    # Root span in trace is not a test
    trace = [root_span]
    assert trace_filter.process_trace(trace) is None


@contextlib.contextmanager
def _patch_dummy_writer():
    original = ddtrace.internal.ci_visibility.recorder.CIVisibilityWriter
    ddtrace.internal.ci_visibility.recorder.CIVisibilityWriter = DummyCIVisibilityWriter
    yield
    ddtrace.internal.ci_visibility.recorder.CIVisibilityWriter = original


def test_ci_visibility_service_enable():

    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        with _patch_dummy_writer():
            dummy_tracer = DummyTracer()
            CIVisibility.enable(tracer=dummy_tracer, service="test-service")
            ci_visibility_instance = CIVisibility._instance
            assert ci_visibility_instance is not None
            assert CIVisibility.enabled
            assert ci_visibility_instance.tracer == dummy_tracer
            assert ci_visibility_instance._service == "test-service"
            assert ci_visibility_instance._code_coverage_enabled_by_api is False
            assert ci_visibility_instance._test_skipping_enabled_by_api is False
            assert any(isinstance(tracer_filter, TraceCiVisibilityFilter) for tracer_filter in dummy_tracer._filters)
            CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_ci_visibility_service_enable_with_app_key_and_itr_disabled(_do_request):
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_APP_KEY="foobar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        _do_request.return_value = Response(
            status=200,
            body='{"data":{"id":"1234","type":"ci_app_tracers_test_service_settings","attributes":'
            '{"code_coverage":true,"tests_skipping":true}}}',
        )
        CIVisibility.enable(service="test-service")
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_ci_visibility_service_enable_with_app_key_and_itr_enabled(_do_request):
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_APP_KEY="foobar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"):
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        _do_request.return_value = Response(
            status=200,
            body='{"data":{"id":"1234","type":"ci_app_tracers_test_service_settings","attributes":'
            '{"code_coverage":true,"tests_skipping":true}}}',
        )
        CIVisibility.enable(service="test-service")
        assert CIVisibility._instance._code_coverage_enabled_by_api is True
        assert CIVisibility._instance._test_skipping_enabled_by_api is True
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_ci_visibility_service_enable_with_app_key_and_error_response(_do_request):
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_APP_KEY="foobar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        _do_request.return_value = Response(
            status=404,
            body='{"errors": ["Not found"]}',
        )
        CIVisibility.enable(service="test-service")
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False
        CIVisibility.disable()


def test_ci_visibility_service_disable():
    with override_env(dict(DD_API_KEY="foobar.baz")):
        with _patch_dummy_writer():
            dummy_tracer = DummyTracer()
            CIVisibility.enable(tracer=dummy_tracer, service="test-service")
            CIVisibility.disable()
            ci_visibility_instance = CIVisibility._instance
            assert ci_visibility_instance is None
            assert not CIVisibility.enabled


@pytest.mark.parametrize(
    "repository_url,repository_name",
    [
        ("https://github.com/DataDog/dd-trace-py.git", "dd-trace-py"),
        ("https://github.com/DataDog/dd-trace-py", "dd-trace-py"),
        ("git@github.com:DataDog/dd-trace-py.git", "dd-trace-py"),
        ("git@github.com:DataDog/dd-trace-py", "dd-trace-py"),
        ("dd-trace-py", "dd-trace-py"),
        ("git@hostname.com:org/repo-name.git", "repo-name"),
        ("git@hostname.com:org/repo-name", "repo-name"),
        ("ssh://git@hostname.com:org/repo-name", "repo-name"),
        ("git+git://github.com/org/repo-name.git", "repo-name"),
        ("git+ssh://github.com/org/repo-name.git", "repo-name"),
        ("git+https://github.com/org/repo-name.git", "repo-name"),
    ],
)
def test_repository_name_extracted(repository_url, repository_name):
    assert _extract_repository_name_from_url(repository_url) == repository_name


def test_repository_name_not_extracted_warning():
    """If provided an invalid repository url, should raise warning and return original repository url"""
    repository_url = "https://github.com:organ[ization/repository-name"
    with mock.patch("ddtrace.internal.ci_visibility.recorder.log") as mock_log:
        extracted_repository_name = _extract_repository_name_from_url(repository_url)
        assert extracted_repository_name == repository_url
    mock_log.warning.assert_called_once_with("Repository name cannot be parsed from repository_url: %s", repository_url)


DUMMY_RESPONSE = Response(status=200, body='{"data": [{"type": "commit", "id": "%s", "attributes": {}}]}' % TEST_SHA)


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_git_client_worker_agentless(_do_request, git_repo):
    _do_request.return_value = Response(
        status=200,
        body='{"data":{"id":"1234","type":"ci_app_tracers_test_service_settings","attributes":'
        '{"code_coverage":true,"tests_skipping":true}}}',
    )
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_APPLICATION_KEY="banana",
            DD_CIVISIBILITY_ITR_ENABLED="1",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        with _patch_dummy_writer():
            dummy_tracer = DummyTracer()
            start_time = time.time()
            with mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"), mock.patch(
                "ddtrace.internal.ci_visibility.recorder._get_git_repo"
            ) as ggr:
                original = ddtrace.internal.ci_visibility.git_client.RESPONSE
                ddtrace.internal.ci_visibility.git_client.RESPONSE = DUMMY_RESPONSE
                ggr.return_value = git_repo
                CIVisibility.enable(tracer=dummy_tracer, service="test-service")
                assert CIVisibility._instance._git_client is not None
                assert CIVisibility._instance._git_client._worker is not None
                assert CIVisibility._instance._git_client._base_url == "https://api.datadoghq.com/api/v2/git"
                CIVisibility.disable()
    shutdown_timeout = dummy_tracer.SHUTDOWN_TIMEOUT
    assert (
        time.time() - start_time <= shutdown_timeout + 0.1
    ), "CIVisibility.disable() should not block for longer than tracer timeout"
    ddtrace.internal.ci_visibility.git_client.RESPONSE = original


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_git_client_worker_evp_proxy(_do_request, git_repo):
    _do_request.return_value = Response(
        status=200,
        body='{"data":{"id":"1234","type":"ci_app_tracers_test_service_settings","attributes":'
        '{"code_coverage":true,"tests_skipping":true}}}',
    )
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_APPLICATION_KEY="banana",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ), mock.patch(
        "ddtrace.internal.ci_visibility.recorder.CIVisibility._agent_evp_proxy_is_available", return_value=True
    ):
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        with _patch_dummy_writer():
            dummy_tracer = DummyTracer()
            start_time = time.time()
            with mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"), mock.patch(
                "ddtrace.internal.ci_visibility.recorder._get_git_repo"
            ) as ggr:
                original = ddtrace.internal.ci_visibility.git_client.RESPONSE
                ddtrace.internal.ci_visibility.git_client.RESPONSE = DUMMY_RESPONSE
                ggr.return_value = git_repo
                CIVisibility.enable(tracer=dummy_tracer, service="test-service")
                assert CIVisibility._instance._git_client is not None
                assert CIVisibility._instance._git_client._worker is not None
                assert CIVisibility._instance._git_client._base_url == "http://localhost:8126/evp_proxy/v2/api/v2/git"
                CIVisibility.disable()
    shutdown_timeout = dummy_tracer.SHUTDOWN_TIMEOUT
    assert (
        time.time() - start_time <= shutdown_timeout + 0.1
    ), "CIVisibility.disable() should not block for longer than tracer timeout"
    ddtrace.internal.ci_visibility.git_client.RESPONSE = original


def test_git_client_get_repository_url(git_repo):
    remote_url = CIVisibilityGitClient._get_repository_url(cwd=git_repo)
    assert remote_url == "git@github.com:test-repo-url.git"


def test_git_client_get_latest_commits(git_repo):
    latest_commits = CIVisibilityGitClient._get_latest_commits(cwd=git_repo)
    assert latest_commits == [TEST_SHA]


def test_git_client_search_commits():
    remote_url = "git@github.com:test-repo-url.git"
    latest_commits = [TEST_SHA]
    serializer = CIVisibilityGitClientSerializerV1("foo", "bar")
    backend_commits = CIVisibilityGitClient._search_commits(
        REQUESTS_MODE.AGENTLESS_EVENTS, "", remote_url, latest_commits, serializer, DUMMY_RESPONSE
    )
    assert latest_commits[0] in backend_commits


def test_get_client_do_request_agentless_headers():
    serializer = CIVisibilityGitClientSerializerV1("foo", "bar")
    response = mock.MagicMock()
    setattr(response, "status", 200)

    with mock.patch("ddtrace.internal.http.HTTPConnection.request") as _request, mock.patch(
        "ddtrace.internal.compat.get_connection_response", return_value=response
    ):
        CIVisibilityGitClient._do_request(
            REQUESTS_MODE.AGENTLESS_EVENTS, "http://base_url", "/endpoint", "payload", serializer, {}
        )

    _request.assert_called_once_with(
        "POST", "http://base_url/repository/endpoint", "payload", {"dd-api-key": "foo", "dd-application-key": "bar"}
    )


def test_get_client_do_request_evp_proxy_headers():
    serializer = CIVisibilityGitClientSerializerV1("foo", "bar")
    response = mock.MagicMock()
    setattr(response, "status", 200)

    with mock.patch("ddtrace.internal.http.HTTPConnection.request") as _request, mock.patch(
        "ddtrace.internal.compat.get_connection_response", return_value=response
    ):
        CIVisibilityGitClient._do_request(
            REQUESTS_MODE.EVP_PROXY_EVENTS, "http://base_url", "/endpoint", "payload", serializer, {}
        )

    _request.assert_called_once_with(
        "POST", "http://base_url/repository/endpoint", "payload", {"X-Datadog-EVP-Subdomain": "api"}
    )


def test_git_client_get_filtered_revisions(git_repo):
    excluded_commits = [TEST_SHA]
    filtered_revisions = CIVisibilityGitClient._get_filtered_revisions(excluded_commits, cwd=git_repo)
    assert filtered_revisions == ""


def test_git_client_build_packfiles(git_repo):
    found_rand = found_idx = found_pack = False
    with CIVisibilityGitClient._build_packfiles("%s\n" % TEST_SHA, cwd=git_repo) as packfiles_path:
        assert packfiles_path
        parts = packfiles_path.split("/")
        directory = "/".join(parts[:-1])
        rand = parts[-1]
        assert os.path.isdir(directory)
        for filename in os.listdir(directory):
            if rand in filename:
                found_rand = True
                if filename.endswith(".idx"):
                    found_idx = True
                elif filename.endswith(".pack"):
                    found_pack = True
            if found_rand and found_idx and found_pack:
                break
        else:
            pytest.fail()
    assert not os.path.isdir(directory)


@mock.patch("ddtrace.ext.git.TemporaryDirectory")
def test_git_client_build_packfiles_temp_dir_value_error(_temp_dir_mock, git_repo):
    _temp_dir_mock.side_effect = ValueError("Invalid cross-device link")
    found_rand = found_idx = found_pack = False
    with CIVisibilityGitClient._build_packfiles("%s\n" % TEST_SHA, cwd=git_repo) as packfiles_path:
        assert packfiles_path
        parts = packfiles_path.split("/")
        directory = "/".join(parts[:-1])
        rand = parts[-1]
        assert os.path.isdir(directory)
        for filename in os.listdir(directory):
            if rand in filename:
                found_rand = True
                if filename.endswith(".idx"):
                    found_idx = True
                elif filename.endswith(".pack"):
                    found_pack = True
            if found_rand and found_idx and found_pack:
                break
        else:
            pytest.fail()
    # CWD is not a temporary dir, so no deleted after using it.
    assert os.path.isdir(directory)


def test_git_client_upload_packfiles(git_repo):
    serializer = CIVisibilityGitClientSerializerV1("foo", "bar")
    remote_url = "git@github.com:test-repo-url.git"
    with CIVisibilityGitClient._build_packfiles("%s\n" % TEST_SHA, cwd=git_repo) as packfiles_path:
        with mock.patch("ddtrace.internal.ci_visibility.git_client.CIVisibilityGitClient._do_request") as dr:
            CIVisibilityGitClient._upload_packfiles(
                REQUESTS_MODE.AGENTLESS_EVENTS, "", remote_url, packfiles_path, serializer, None, cwd=git_repo
            )
            assert dr.call_count == 1
            call_args = dr.call_args_list[0][0]
            call_kwargs = dr.call_args.kwargs
            assert call_args[0] == REQUESTS_MODE.AGENTLESS_EVENTS
            assert call_args[1] == ""
            assert call_args[2] == "/packfile"
            assert call_args[3].startswith(b"------------boundary------\r\nContent-Disposition: form-data;")
            assert call_kwargs["headers"] == {"Content-Type": "multipart/form-data; boundary=----------boundary------"}


def test_civisibilitywriter_agentless_url():
    with override_env(dict(DD_API_KEY="foobar.baz")):
        with override_global_config({"_ci_visibility_agentless_url": "https://foo.bar"}):
            dummy_writer = DummyCIVisibilityWriter()
            assert dummy_writer.intake_url == "https://foo.bar"


def test_civisibilitywriter_coverage_agentless_url():
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.writer.coverage_enabled", return_value=True):
        dummy_writer = DummyCIVisibilityWriter()
        assert dummy_writer.intake_url == "https://citestcycle-intake.datadoghq.com"

        cov_client = dummy_writer._clients[1]
        assert cov_client._intake_url == "https://citestcov-intake.datadoghq.com"

        with mock.patch("ddtrace.internal.writer.writer.get_connection") as _get_connection:
            dummy_writer._put("", {}, cov_client)
            _get_connection.assert_called_once_with("https://citestcov-intake.datadoghq.com", 2.0)


def test_civisibilitywriter_coverage_evp_proxy_url():
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.writer.coverage_enabled", return_value=True):
        dummy_writer = DummyCIVisibilityWriter(use_evp=True)

        test_client = dummy_writer._clients[0]
        assert test_client.ENDPOINT == "/evp_proxy/v2/api/v2/citestcycle"
        cov_client = dummy_writer._clients[1]
        assert cov_client.ENDPOINT == "/evp_proxy/v2/api/v2/citestcov"

        with mock.patch("ddtrace.internal.writer.writer.get_connection") as _get_connection:
            dummy_writer._put("", {}, cov_client)
            _get_connection.assert_called_once_with("http://localhost:8126", 2.0)


def test_civisibilitywriter_agentless_url_envvar():
    with override_env(
        dict(
            DD_API_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()
        assert CIVisibility._instance._requests_mode == REQUESTS_MODE.AGENTLESS_EVENTS
        assert CIVisibility._instance.tracer._writer.intake_url == "https://foo.bar"
        CIVisibility.disable()


def test_civisibilitywriter_evp_proxy_url():
    with override_env(dict(DD_API_KEY="foobar.baz",)), mock.patch(
        "ddtrace.internal.ci_visibility.recorder.CIVisibility._agent_evp_proxy_is_available", return_value=True
    ):
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()
        assert CIVisibility._instance._requests_mode == REQUESTS_MODE.EVP_PROXY_EVENTS
        assert CIVisibility._instance.tracer._writer.intake_url == "http://localhost:8126"
        CIVisibility.disable()


def test_civisibilitywriter_only_traces():
    with override_env(dict(DD_API_KEY="foobar.baz",)), mock.patch(
        "ddtrace.internal.ci_visibility.recorder.CIVisibility._agent_evp_proxy_is_available", return_value=False
    ):
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()
        assert CIVisibility._instance._requests_mode == REQUESTS_MODE.TRACES
        assert CIVisibility._instance.tracer._writer.intake_url == "http://localhost:8126"
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_no_app_key_request_not_called(_do_request):
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ):
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()

        _do_request.assert_not_called()
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_itr_disabled_request_not_called(_do_request):
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_APP_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
        )
    ):
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()

        _do_request.assert_not_called()
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False

        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_itr_enabled_request_called(_do_request):
    _do_request.return_value = Response(
        status=200,
        body='{"data":{"id":"1234","type":"ci_app_tracers_test_service_settings","attributes":'
        '{"code_coverage":true,"tests_skipping":true}}}',
    )
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_APP_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
            DD_ENV="staging",
            DD_GIT_COMMIT_SHA="fffffff",
            DD_GIT_BRANCH="main",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibility._fetch_tests_to_skip"), mock.patch(
        "ddtrace.internal.ci_visibility.recorder.CIVisibilityGitClient.start"
    ) as git_start, mock.patch(
        "ddtrace.internal.ci_visibility.recorder.uuid4"
    ) as _uuid4:
        _uuid4.return_value = "111-111-111"
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable(service="test-service")

        _do_request.assert_called_with(
            "POST",
            "https://api.datadoghq.com/api/v2/libraries/tests/services/setting",
            json.dumps(
                {
                    "data": {
                        "id": "111-111-111",
                        "type": "ci_app_test_service_libraries_settings",
                        "attributes": {
                            "service": "test-service",
                            "env": "staging",
                            "repository_url": "git@github.com:DataDog/dd-trace-py.git",
                            "sha": "fffffff",
                            "branch": "main",
                        },
                    }
                }
            ),
            {"dd-api-key": "foo.bar", "dd-application-key": "foobar.baz", "Content-Type": "application/json"},
        )
        assert CIVisibility._instance._code_coverage_enabled_by_api is True
        assert CIVisibility._instance._test_skipping_enabled_by_api is True

        # Git client is started
        assert git_start.call_count == 1
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_itr_enabled_errors_not_found(_do_request):
    _do_request.return_value = Response(
        status=200,
        body='{"errors":["Not found"]}',
    )
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_APP_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibilityGitClient.start") as git_start:
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()

        _do_request.assert_called()
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False

        # Git client is started
        assert git_start.call_count == 1
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_itr_enabled_404_response(_do_request):
    _do_request.return_value = Response(
        status=404,
        body="",
    )
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_APP_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.recorder.CIVisibilityGitClient.start") as git_start:
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()

        code_cov_enabled, itr_enabled = CIVisibility._instance._check_enabled_features()

        _do_request.assert_called()
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False

        # Git client is started
        assert git_start.call_count == 1
        CIVisibility.disable()


@mock.patch("ddtrace.internal.ci_visibility.recorder._do_request")
def test_civisibility_check_enabled_features_itr_enabled_malformed_response(_do_request):
    _do_request.return_value = Response(
        status=200,
        body="}",
    )
    with override_env(
        dict(
            DD_API_KEY="foo.bar",
            DD_APP_KEY="foobar.baz",
            DD_CIVISIBILITY_AGENTLESS_URL="https://foo.bar",
            DD_CIVISIBILITY_AGENTLESS_ENABLED="1",
            DD_CIVISIBILITY_ITR_ENABLED="1",
        )
    ), mock.patch("ddtrace.internal.ci_visibility.recorder.log") as mock_log, mock.patch(
        "ddtrace.internal.ci_visibility.recorder.CIVisibilityGitClient.start"
    ) as git_start:
        ddtrace.internal.ci_visibility.writer.config = ddtrace.settings.Config()
        ddtrace.internal.ci_visibility.recorder.ddconfig = ddtrace.settings.Config()
        CIVisibility.enable()

        _do_request.assert_called()
        assert CIVisibility._instance._code_coverage_enabled_by_api is False
        assert CIVisibility._instance._test_skipping_enabled_by_api is False

        # Git client is started
        assert git_start.call_count == 1

        mock_log.warning.assert_called_with("Settings request responded with invalid JSON '%s'", "}")
        CIVisibility.disable()

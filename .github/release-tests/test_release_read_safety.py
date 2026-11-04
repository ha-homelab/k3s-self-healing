"""Focused canonical release GET/freshness backport tests; no live requests."""

import gc
import importlib.util
import os

# Only constructs completed-process and timeout fixtures; execution is mocked.
import subprocess  # nosec B404
import unittest
import weakref
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "release_control_backport", Path(__file__).resolve().parents[2] / "scripts/release_control.py"
)
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)
REPO = "example/project"
SHA = "a" * 40


def http_response(status=0, stdout=b"{}", error_format="gh: API failed (HTTP {status})"):
    """Return a complete gh --include fixture independent of retry support."""
    return subprocess.CompletedProcess(
        [],
        bool(status),
        f"HTTP/2.0 {status or 200} Test\nContent-Type: application/json\r\n\r\n".encode() + stdout,
        error_format.format(status=status).encode() if status else b"",
    )


class WholeReadRecoveryTests(unittest.TestCase):
    """Bounded complete reads recover only from recognized transport failures."""

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}))
        self.gh = rc.GitHub(REPO)

    @staticmethod
    def failed(stderr=b"unexpected EOF", stdout=b"", code=1):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_retry_releases_failed_response_before_waiting_or_recapturing(self):
        def check_case(path, status, framed):
            previous = []

            def assert_released(*_args):
                gc.collect()
                self.assertTrue(previous[0]() is None, "failed stdout is still retained")

            def capture(*_args, **_kwargs):
                if previous:
                    assert_released()
                    return http_response(stdout=b"COMPLETE")
                payload = b"x" * (1024 * 1024)
                if framed:
                    payload = f"HTTP/2.0 {status} Test\r\n\r\n".encode() + payload
                failed = self.failed(stdout=payload)
                previous.append(weakref.ref(failed))
                return failed

            with (
                patch.object(rc.subprocess, "run", side_effect=capture) as run,
                patch.object(rc.time, "sleep", side_effect=assert_released) as sleep,
            ):
                self.assertEqual(self.gh.binary(path), b"COMPLETE")
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once()

        for path, status, framed in (
            ("releases/assets/7", 200, True),
            ("actions/artifacts/7/zip", 503, True),
            ("releases/assets/7", 200, False),
        ):
            with self.subTest(path=path, status=status, framed=framed):
                check_case(path, status, framed)

    def test_known_transport_losses_retry_whole_metadata_response(self):
        for message in (
            b"unexpected EOF",
            b"EOF",
            b"connection reset by peer",
            b"i/o timeout",
            b"TLS handshake timeout",
            b"context deadline exceeded",
            b"Client.Timeout exceeded",
            b"server closed idle connection",
            b"use of closed network connection",
        ):
            with (
                self.subTest(message=message),
                patch.object(
                    rc.subprocess,
                    "run",
                    side_effect=[self.failed(message), http_response(stdout=b'{"id":7}')],
                ) as run,
                patch.object(rc.time, "sleep"),
            ):
                self.assertEqual(self.gh.api("releases/7"), {"id": 7})
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(x.kwargs["timeout"] == 30 for x in run.call_args_list))

    def test_whole_asset_and_zip_retry_discard_failed_binary_prefix(self):
        partial = self.failed(stdout=http_response(stdout=b"PARTIAL").stdout)
        payload = bytes(range(256)) + b"HTTP/2.0 404 body data"
        for path in ("releases/assets/7", "actions/artifacts/7/zip"):
            with (
                self.subTest(path=path),
                patch.object(
                    rc.subprocess, "run", side_effect=[partial, http_response(stdout=payload)]
                ) as run,
                patch.object(rc.time, "sleep"),
            ):
                self.assertEqual(self.gh.binary(path), payload)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(x.kwargs["timeout"] == 120 for x in run.call_args_list))
                self.assertEqual(run.call_args_list[0], run.call_args_list[1])
                self.assertEqual(
                    "Accept: application/octet-stream" in run.call_args.args[0],
                    path.startswith("releases/"),
                )

    def test_empty_successful_binary_is_not_a_retry_signal(self):
        for replies, expected_calls in (
            ([http_response(stdout=b"")], 1),
            ([self.failed(), http_response(stdout=b"")], 2),
        ):
            with (
                self.subTest(expected_calls=expected_calls),
                patch.object(rc.subprocess, "run", side_effect=replies) as run,
                patch.object(rc.time, "sleep"),
            ):
                self.assertEqual(self.gh.binary("releases/assets/7"), b"")
                self.assertEqual(run.call_count, expected_calls)

    def test_repeated_asset_timeouts_are_bounded(self):
        with (
            patch.object(
                rc.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["gh"], 120, output=b"partial"),
            ) as run,
            patch.object(rc.time, "sleep") as sleep,
            self.assertRaises(rc.GitHubError),
        ):
            self.gh.binary("releases/assets/7")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(x.kwargs["timeout"] == 120 for x in run.call_args_list))

    def test_binary_5xx_retries_but_other_statuses_never_do(self):
        for status in (500, 502, 503, 504):
            with (
                self.subTest(status=status),
                patch.object(
                    rc.subprocess,
                    "run",
                    side_effect=[http_response(status), http_response(stdout=b"good")],
                ) as run,
                patch.object(rc.time, "sleep"),
            ):
                self.assertEqual(self.gh.binary("releases/assets/7"), b"good")
                self.assertEqual(run.call_count, 2)
        for status in (400, 401, 403, 404, 409, 422, 429, 501):
            with (
                self.subTest(status=status),
                patch.object(rc.subprocess, "run", return_value=http_response(status)) as run,
            ):
                with self.assertRaises(rc.GitHubError):
                    self.gh.binary("releases/assets/7")
                run.assert_called_once()

    def test_setup_isolates_and_restores_publication_probe_environment(self):
        """An inherited publication setting must never trigger a live probe."""
        case = type(self)()
        with (
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"}),
            patch.object(
                rc.GitHub,
                "verify_publication_permissions",
                side_effect=AssertionError("live probe"),
            ) as probe,
        ):
            try:
                case.setUp()
                probe.assert_not_called()
                self.assertEqual(os.environ["RELEASE_REQUIRE_WORKFLOW_SCOPE"], "")
            finally:
                case.doCleanups()
            self.assertEqual(os.environ["RELEASE_REQUIRE_WORKFLOW_SCOPE"], "true")

    def test_auth_certificate_and_partial_denials_veto_transport_retry(self):
        for message in (
            b"Unauthorized; unexpected EOF",
            b"unauthorised; i/o timeout",
            b"authorization failed; EOF",
            b"UNAUTHORIZED; unexpected EOF",
            b"requires authentication; unexpected EOF",
            b"bad credentials; i/o timeout",
            b"x509 certificate error; unexpected EOF",
            b"permission denied; EOF",
            b"rate limit; EOF",
        ):
            for result in (
                self.failed(message),
                subprocess.TimeoutExpired(["gh"], 30, stderr=message),
            ):
                with (
                    self.subTest(message=message, result=type(result).__name__),
                    patch.object(rc.subprocess, "run", side_effect=[result]) as run,
                ):
                    with self.assertRaises(rc.GitHubError):
                        self.gh.api("releases")
                    run.assert_called_once()
        for status in (401, 403, 404, 429):
            result = self.failed(stdout=f"HTTP/2.0 {status} Denied\r\n".encode())
            with (
                self.subTest(status=status),
                patch.object(rc.subprocess, "run", return_value=result) as run,
            ):
                with self.assertRaises(rc.GitHubError) as error:
                    self.gh.optional("releases/7")
                self.assertFalse(error.exception.not_found)
                run.assert_called_once()

    def test_successful_exit_with_malformed_content_never_retries(self):
        for result in (self.failed(code=0), http_response(stdout=b"{broken")):
            with (
                self.subTest(result=result),
                patch.object(rc.subprocess, "run", return_value=result) as run,
            ):
                with self.assertRaises(rc.ReleaseError):
                    self.gh.api("releases")
                run.assert_called_once()
        malformed = http_response(stdout=b"[]\nHTTP/2.0 200 OK\r\n\r\n{broken")
        with patch.object(rc.subprocess, "run", return_value=malformed) as run:
            with self.assertRaises(rc.GitHubError):
                self.gh.pages("releases")
            run.assert_called_once()

    def test_unknown_cli_failures_and_success_code_5xx_never_retry(self):
        for result in (
            self.failed(b"unknown CLI failure"),
            self.failed(b"unexpected EOF", code=2),
            self.failed(stdout=http_response(503).stdout, code=0),
        ):
            with (
                self.subTest(result=result),
                patch.object(rc.subprocess, "run", return_value=result) as run,
            ):
                with self.assertRaises(rc.GitHubError):
                    self.gh.api("releases")
                run.assert_called_once()

    def test_transport_retry_discards_all_paginated_prefixes(self):
        partial = self.failed(
            stdout=http_response(stdout=b'[{"id":1}]\nHTTP/2.0 200 OK\r\n').stdout
        )
        with (
            patch.object(
                rc.subprocess, "run", side_effect=[partial, http_response(stdout=b'[{"id":2}]')]
            ) as run,
            patch.object(rc.time, "sleep"),
        ):
            self.assertEqual(self.gh.pages("releases"), [{"id": 2}])
            self.assertEqual(run.call_count, 2)


class CurrentAttemptFreshnessTests(unittest.TestCase):
    """Successful retry evidence must belong to the exact current attempt."""

    def setUp(self):
        self.detail = {
            "id": 123,
            "run_attempt": 2,
            "head_sha": SHA,
            "path": rc.WORKFLOW,
            "run_started_at": "2026-11-03T02:00:00Z",
            "created_at": "2026-11-03T01:00:00Z",
        }
        self.job = {
            "name": "checks / CI gate",
            "status": "completed",
            "conclusion": "success",
            "head_sha": SHA,
            "run_attempt": 2,
            "started_at": "2026-11-03T02:01:00Z",
            "completed_at": "2026-11-03T02:05:00Z",
            "steps": [
                {
                    "name": "Check",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-11-03T02:02:00Z",
                    "completed_at": "2026-11-03T02:04:00Z",
                }
            ],
        }
        self.gh = Mock()
        self.gh.api.return_value = self.detail

    def check(self, jobs=None):
        rc.require_current_attempt_jobs(self.gh, 123, SHA, 2, [self.job] if jobs is None else jobs)

    def test_fresh_execution_and_old_run_creation_are_accepted(self):
        self.check()
        self.gh.api.assert_called_once_with("actions/runs/123/attempts/2")

    def test_fresh_attempt_uses_the_real_allowlisted_transport(self):
        """Exercise API shape validation as well as the execution-time guard."""
        with (
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}),
            patch.object(
                rc.subprocess, "run", return_value=http_response(0, rc.json_bytes(self.detail))
            ) as command,
        ):
            gh = rc.GitHub(REPO)
            rc.require_current_attempt_jobs(gh, 123, SHA, 2, [self.job])
        command.assert_called_once()
        self.assertEqual(
            command.call_args.args[0],
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--include",
                "--method",
                "GET",
                "--",
                f"repos/{REPO}/actions/runs/123/attempts/2",
            ],
        )
        self.assertEqual(command.call_args.kwargs["timeout"], 30)

    def test_timezone_offsets_compare_as_instants(self):
        self.detail["run_started_at"] = "2026-11-02T18:00:00-08:00"
        self.check()

    def test_first_attempt_preserves_historical_missing_clock_contract(self):
        rc.require_current_attempt_jobs(self.gh, 123, SHA, 1, [{"conclusion": "success"}])
        self.gh.api.assert_not_called()

    def test_cloned_success_rejected_even_with_new_attempt_number(self):
        self.job.update(started_at="2026-11-03T01:01:00Z", completed_at="2026-11-03T01:05:00Z")
        with self.assertRaisesRegex(rc.ReleaseError, "predates"):
            self.check()

    def test_old_steps_cannot_hide_under_new_job_timestamps(self):
        self.job["steps"][0].update(
            started_at="2026-11-03T01:01:00Z", completed_at="2026-11-03T01:05:00Z"
        )
        with self.assertRaisesRegex(rc.ReleaseError, "outside"):
            self.check()

    def test_intentional_skips_and_unfinished_publisher_do_not_need_clocks(self):
        self.job["steps"].append({"conclusion": "skipped"})
        self.job["steps"].append({"conclusion": "failure", "status": "completed"})
        self.check(
            [
                self.job,
                {"status": "completed", "conclusion": "skipped"},
                {"status": "in_progress", "conclusion": None},
            ]
        )

    def test_wrong_attempt_metadata_is_rejected(self):
        for key, value in [
            ("id", 124),
            ("head_sha", "b" * 40),
            ("run_attempt", 1),
            ("path", "wrong.yml"),
        ]:
            with self.subTest(key=key), self.assertRaisesRegex(rc.ReleaseError, "identity"):
                self.gh.api.return_value = self.detail | {key: value}
                self.check()

    def test_malformed_attempt_metadata_is_actionable(self):
        for bad in [None, [], "invalid", 2, True]:
            with (
                self.subTest(value=bad),
                self.assertRaisesRegex(rc.ReleaseError, "Malformed release attempt metadata"),
            ):
                self.gh.api.return_value = bad
                self.check()

    def test_successful_job_needs_executed_success_step(self):
        self.job["steps"] = [{"status": "completed", "conclusion": "skipped"}]
        with self.assertRaisesRegex(rc.ReleaseError, "lacks successful execution steps"):
            self.check()

    def test_wrong_successful_job_identity_is_rejected(self):
        for key, value in [("head_sha", "b" * 40), ("run_attempt", 1), ("head_sha", None)]:
            with self.subTest(key=key), self.assertRaisesRegex(rc.ReleaseError, "differs"):
                self.check([self.job | {key: value}])

    def test_missing_naive_invalid_and_non_string_timestamps_are_rejected(self):
        for bad in [
            None,
            "",
            0,
            "2026-11-03T02:00:00",
            "2026-99-03T02:00:00Z",
            "2026-11-03T02:00:00+00:60",
            "2026-11-03T02:00:00+24:00",
            "now",
        ]:
            for target, key in [
                ("detail", "run_started_at"),
                ("job", "started_at"),
                ("job", "completed_at"),
                ("step", "started_at"),
                ("step", "completed_at"),
            ]:
                with (
                    self.subTest(target=target, key=key, bad=bad),
                    self.assertRaises(rc.ReleaseError),
                ):
                    detail, job = deepcopy(self.detail), deepcopy(self.job)
                    (detail if target == "detail" else job if target == "job" else job["steps"][0])[
                        key
                    ] = bad
                    self.gh.api.return_value = detail
                    self.check([job])
        self.gh.api.return_value = self.detail

    def test_timestamp_shape_rejects_non_ascii_digits(self):
        for value in [
            "٢٠٢٦-11-03T02:00:00Z",
            "2026-11-03T٠٢:00:00Z",
            "2026-11-03T02:00:00.٥Z",
            "2026-11-03T02:00:00+٠٠:00",
        ]:
            with self.subTest(value=value), self.assertRaises(rc.ReleaseError):
                rc._execution_timestamp(value, "test timestamp")

    def test_reversed_intervals_and_missing_steps_are_rejected(self):
        for changes in [
            {"completed_at": "2026-11-03T02:00:00Z"},
            {"steps": []},
            {"steps": None},
            {"steps": [{"conclusion": "success", "status": "queued"}]},
            {"steps": [None]},
        ]:
            with self.subTest(changes=changes), self.assertRaises(rc.ReleaseError):
                self.check([self.job | changes])


class AttemptRouteTests(unittest.TestCase):
    """Attempt metadata reads never broaden the allowed GitHub API surface."""

    def test_attempt_detail_route_rejects_invalid_ids_and_write_methods(self):
        """The added metadata route grants no write, query or traversal surface."""
        with patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}):
            gh = rc.GitHub(REPO)
        cases = [
            (path, "GET", None)
            for path in (
                "actions/runs/0/attempts/2",
                "actions/runs/123/attempts/0",
                "actions/runs/0123/attempts/2",
                "actions/runs/123/attempts/02",
                "actions/runs/123/attempts/2/",
                "actions/runs/123/attempts/2?x=1",
                "actions/runs/123/attempts/2/artifacts",
                "actions/runs/123/attempts/%32",
                "actions/runs/١٢٣/attempts/2",
                "actions/runs/123/attempts/٢",
                "actions/runs/1١/attempts/2",
                "actions/runs/123/attempts/2٢",
            )
        ]
        cases += [
            ("actions/runs/123/attempts/2", method, {})
            for method in ("POST", "PATCH", "PUT", "DELETE")
        ]
        for path, method, body in cases:
            with (
                self.subTest(path=path, method=method),
                patch.object(rc.subprocess, "run") as command,
            ):
                with self.assertRaises(rc.ReleaseError):
                    gh.request(path, method, body)
                command.assert_not_called()


class GateIntegrationTests(unittest.TestCase):
    """Publication gates reject stale evidence through the real integration path."""

    def test_gate_rejects_cloned_validation_and_accepts_fresh_execution(self):
        detail = {
            "id": 123,
            "run_attempt": 2,
            "head_sha": SHA,
            "path": rc.WORKFLOW,
            "run_started_at": "2026-11-03T02:00:00Z",
        }
        gate = {
            "name": "Release gate",
            "status": "completed",
            "conclusion": "success",
            "head_sha": SHA,
            "run_attempt": 2,
            "started_at": "2026-11-03T02:01:00Z",
            "completed_at": "2026-11-03T02:05:00Z",
            "steps": [
                {
                    "name": "Check",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-11-03T02:02:00Z",
                    "completed_at": "2026-11-03T02:04:00Z",
                }
            ],
        }
        validation = deepcopy(gate)
        validation.update(
            name="checks / CI gate",
            started_at="2026-11-03T01:01:00Z",
            completed_at="2026-11-03T01:05:00Z",
        )
        gh = Mock()
        gh.api.return_value = detail
        gh.pages.return_value = [gate, validation]
        with self.assertRaisesRegex(rc.ReleaseError, "predates"):
            rc.release_gate_ready(gh, 123, SHA, 2)
        gh.pages.assert_called_once_with("actions/runs/123/attempts/2/jobs", "jobs")
        validation.update(started_at=gate["started_at"], completed_at=gate["completed_at"])
        self.assertTrue(rc.release_gate_ready(gh, 123, SHA, 2))


if __name__ == "__main__":
    unittest.main()

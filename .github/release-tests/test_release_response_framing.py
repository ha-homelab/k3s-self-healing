"""Partial response framing regressions; all HTTP responses are local fixtures."""

import importlib.util
import json
import os

# Only constructs completed-process and timeout fixtures; execution is mocked.
import subprocess  # nosec B404
import unittest
from itertools import product
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "release_control_response_framing",
    Path(__file__).resolve().parents[2] / "scripts/release_control.py",
)
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)


def http_response(status=0, stdout=b"{}"):
    """Create a complete gh --include response with deterministic header bytes."""
    return subprocess.CompletedProcess(
        [],
        bool(status),
        f"HTTP/2.0 {status or 200} Test\nContent-Type: application/json\r\n\r\n".encode()
        + stdout,
        f"gh: API failed (HTTP {status})".encode() if status else b"",
    )


class ResponseFramingTests(unittest.TestCase):
    """Body text cannot masquerade as a denial or authorize an absent resource."""

    def setUp(self):
        self.enterContext(
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""})
        )
        self.gh = rc.GitHub("example/project")

    @staticmethod
    def failed(stderr=b"unexpected EOF", stdout=b"", code=1):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_partial_binary_body_status_lines_do_not_veto_whole_retry(self):
        for path, newline, status, failure in product(
            ("releases/assets/7", "actions/artifacts/7/zip"),
            (b"\n", b"\r\n"),
            (401, 403, 404, 429),
            ("timeout", "eof"),
        ):
            with self.subTest(
                path=path, newline=newline, status=status, failure=failure
            ):
                header = b"HTTP/2.0 200 OK" + newline
                partial = header + newline + b"partial binary" + newline
                partial += f"HTTP/2.0 {status} body data".encode() + newline
                result = (
                    subprocess.TimeoutExpired(["gh"], 120, output=partial)
                    if failure == "timeout"
                    else self.failed(stdout=partial)
                )
                with (
                    patch.object(
                        rc.subprocess,
                        "run",
                        side_effect=[result, http_response(stdout=b"COMPLETE")],
                    ) as run,
                    patch.object(rc.time, "sleep") as sleep,
                    patch.object(
                        rc, "_github_http_head", wraps=rc._github_http_head
                    ) as parse_header,
                ):
                    self.assertEqual(self.gh.binary(path), b"COMPLETE")
                    if failure == "timeout":
                        self.assertEqual(
                            parse_header.call_args_list[0].args[0], header + newline
                        )
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(sleep.call_count, 1)
                    self.assertEqual(run.call_args_list[0], run.call_args_list[1])

    def test_timed_out_first_header_denials_still_veto_retry(self):
        for newline, status, complete_header in product(
            (b"\n", b"\r\n"),
            (401, 403, 404, 429),
            (False, True),
        ):
            with self.subTest(
                newline=newline, status=status, complete_header=complete_header
            ):
                partial = f"HTTP/2.0 {status} Denied".encode() + newline
                if complete_header:
                    partial += newline + b"partial response body"
                result = subprocess.TimeoutExpired(["gh"], 30, output=partial)
                with (
                    patch.object(rc.subprocess, "run", side_effect=[result]) as run,
                    patch.object(rc.time, "sleep") as sleep,
                ):
                    with self.assertRaises(rc.GitHubError) as error:
                        self.gh.optional("releases/7")
                    self.assertFalse(error.exception.not_found)
                    run.assert_called_once()
                    sleep.assert_not_called()

    def test_later_partial_page_denials_still_veto_retry(self):
        for status, failure in product((401, 403, 404, 429), ("timeout", "eof")):
            with self.subTest(status=status, failure=failure):
                partial = http_response(stdout=b'[{"id":1}]\n').stdout
                partial += f"HTTP/2.0 {status} Denied\r\n".encode()
                result = (
                    subprocess.TimeoutExpired(["gh"], 30, output=partial)
                    if failure == "timeout"
                    else self.failed(stdout=partial)
                )
                with (
                    patch.object(rc.subprocess, "run", side_effect=[result]) as run,
                    patch.object(rc.time, "sleep") as sleep,
                ):
                    with self.assertRaises(rc.GitHubError) as error:
                        self.gh.pages("releases")
                    self.assertFalse(error.exception.not_found)
                    run.assert_called_once()
                    sleep.assert_not_called()

    def test_escaped_paginated_json_status_text_does_not_veto_retry(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                body = json.dumps([{"text": f"body\nHTTP/2.0 {status} data"}]).encode()
                result = subprocess.TimeoutExpired(
                    ["gh"], 30, output=http_response(stdout=body).stdout
                )
                with (
                    patch.object(
                        rc.subprocess,
                        "run",
                        side_effect=[result, http_response(stdout=b'[{"id":2}]')],
                    ) as run,
                    patch.object(rc.time, "sleep"),
                ):
                    self.assertEqual(self.gh.pages("releases"), [{"id": 2}])
                    self.assertEqual(run.call_count, 2)

    def test_timed_out_body_status_cannot_establish_optional_absence(self):
        partial = http_response(stdout=b"partial\nHTTP/2.0 404 body data").stdout
        result = subprocess.TimeoutExpired(["gh"], 30, output=partial)
        with (
            patch.object(
                rc.subprocess,
                "run",
                side_effect=[result, http_response(stdout=b'{"id":7}')],
            ) as run,
            patch.object(rc.time, "sleep"),
        ):
            self.assertEqual(self.gh.optional("releases/7"), {"id": 7})
            self.assertEqual(run.call_count, 2)

    def test_unproven_header_boundary_keeps_conservative_denial_veto(self):
        for partial in (
            b"unknown\n\nHTTP/2.0 403 body data",
            b"HTTP/2.0 200 OK\nInvalid header\n\nHTTP/2.0 403 body data",
            b"HTTP/2.0 200 OK\nHTTP/2.0 403 partial header",
        ):
            for failure in ("timeout", "eof"):
                with self.subTest(partial=partial, failure=failure):
                    result = (
                        subprocess.TimeoutExpired(["gh"], 120, output=partial)
                        if failure == "timeout"
                        else self.failed(stdout=partial)
                    )
                    with (
                        patch.object(rc.subprocess, "run", side_effect=[result]) as run,
                        patch.object(rc.time, "sleep") as sleep,
                    ):
                        with self.assertRaises(rc.GitHubError) as error:
                            self.gh.binary("releases/assets/7")
                        self.assertFalse(error.exception.not_found)
                        run.assert_called_once()
                        sleep.assert_not_called()

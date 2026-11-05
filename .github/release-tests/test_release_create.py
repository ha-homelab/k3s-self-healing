# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Offline loss/denial and same-call attribution regressions; no GitHub access."""

import base64
import copy
import subprocess
import unittest
from unittest.mock import Mock, patch

import test_versioned_release as versioned

rc = versioned.rc
lifecycle = versioned.lifecycle
REPO = versioned.REPO
SHA = versioned.SHA


def wire(status, body):
    return f"HTTP/2.0 {status} Response\r\nContent-Type: application/json\r\n\r\n".encode() + body


class CreateTransportTests(unittest.TestCase):
    def setUp(self):
        self.gh = rc.GitHub(REPO)

    def call(self, status, content=b'{}', code=0, stderr=b''):
        raw = wire(status, content) if status else content
        result = subprocess.CompletedProcess([], code, raw, stderr)
        with patch.object(rc.subprocess, "run", return_value=result) as call:
            try:
                return self.gh.create_release_once({"draft": True})
            finally:
                call.assert_called_once()
                args, kwargs = call.call_args
                self.assertEqual(args[0][-2:], ["--", f"repos/{REPO}/releases"])
                self.assertEqual(args[0][args[0].index("--method") + 1], "POST")
                self.assertEqual(kwargs["timeout"], 120)

    def test_normal_create(self):
        self.assertEqual(self.call(201, b'{"id":42}'), {"id": 42})

    def test_only_typed_ambiguous_responses(self):
        cases = [(None, b'', 1, b'Post: EOF'),
                 (None, b'', 1, b'unexpected end of JSON input'),
                 (201, b'{"id":', 0, b''), (201, b'', 1, b'Post: EOF'),
                 (500, b'{}', 1, b'server error'), (502, b'', 1, b''),
                 (503, b'', 1, b''), (504, b'', 1, b'')]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(rc.AmbiguousCreate):
                self.call(*case)

    def test_denial_unknown_and_semantics_do_not_reconcile(self):
        cases = [(status, b'{}', 1, b'Post: EOF') for status in (400, 401, 403, 404, 408, 409, 422, 429)]
        cases += [(None, b'', 1, b'Post: EOF certificate error'),
                  (None, b'HTTP/2.0 422 Rejected\r\n', 1, b'Post: EOF'),
                  (None, b'', 1, b'unrecognized failure'), (200, b'{}', 0, b''),
                  (201, b'[]', 0, b''), (201, b'{"id":1,"id":2}', 0, b'')]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(rc.ReleaseError) as caught:
                self.call(*case)
            self.assertNotIsInstance(caught.exception, rc.AmbiguousCreate)

    def test_timeout_is_not_a_write_retry_and_denial_vetoes(self):
        for diagnostic in (b'', b'permission denied'):
            error = subprocess.TimeoutExpired(['gh'], 120, stderr=diagnostic)
            expected = rc.GitHubError if diagnostic else rc.AmbiguousCreate
            with patch.object(rc.subprocess, 'run', side_effect=error) as call:
                with self.assertRaises(expected) as caught:
                    self.gh.create_release_once({"draft": True})
                call.assert_called_once()
            if diagnostic:
                self.assertNotIsInstance(caught.exception, rc.AmbiguousCreate)


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.reader = rc.CreateReadback(REPO)

    def test_fixed_get_and_remaining_budget(self):
        self.reader.deadline = 115
        response = subprocess.CompletedProcess([], 0, wire(200, b'{"id":42}'), b'')
        with (patch.object(rc.time, 'monotonic', side_effect=[100, 101]),
              patch.object(rc.subprocess, 'run', return_value=response) as call):
            self.assertEqual(self.reader.json('releases/42'), {'id': 42})
        args, kwargs = call.call_args
        self.assertEqual(args[0][args[0].index('--method') + 1], 'GET')
        self.assertNotIn('--input', args[0])
        self.assertEqual(kwargs['timeout'], 15)
        self.assertEqual(self.reader.deadline, 115)
        call.assert_called_once()

    def test_explicit_pagination_and_duplicate_refusal(self):
        header = f'Link: <https://api.github.com/repos/{REPO}/releases?per_page=100&page=2>; rel="next"\r\n'.encode()
        with patch.object(self.reader, 'get', side_effect=[([{'id': 1}], header), ([{'id': 2}], b'')]):
            self.assertEqual(len(self.reader.pages('releases')), 2)
        with patch.object(self.reader, 'get', side_effect=[([{'id': 1}], header), ([{'id': 1}], b'')]):
            with self.assertRaisesRegex(rc.ReleaseError, 'Duplicate'):
                self.reader.pages('releases')

    def test_foreign_skipped_and_duplicate_next_links_fail(self):
        urls = [f'https://evil.test/repos/{REPO}/releases?per_page=100&page=2',
                f'https://api.github.com/repos/{REPO}/releases?per_page=100&page=3',
                f'https://api.github.com/repos/{REPO}/releases?per_page=100&page=2&page=2']
        for url in urls:
            with self.subTest(url=url), patch.object(self.reader, 'get', return_value=(
                    [{'id': 1}], f'Link: <{url}>; rel="next"\r\n'.encode())):
                with self.assertRaises(rc.ReleaseError):
                    self.reader.pages('releases')

    def test_full_page_without_link_requires_an_extra_page(self):
        full = [{'id': n} for n in range(1, 101)]
        with patch.object(self.reader, 'get', side_effect=[(full, b''), ([], b'')]) as get:
            self.assertEqual(len(self.reader.pages('releases')), 100)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(get.call_args.args, ('releases?per_page=100&page=2',))
        with patch.object(self.reader, 'get', side_effect=[(full, b''), rc.ReleaseError('missing page')]):
            with self.assertRaises(rc.ReleaseError):
                self.reader.pages('releases')

    def test_one_clock_request_and_byte_bounds(self):
        with patch.object(rc.time, 'monotonic', return_value=self.reader.deadline):
            with patch.object(rc.subprocess, 'run') as call, self.assertRaises(rc.ReleaseError):
                self.reader.json('releases')
            call.assert_not_called()
        self.reader.calls = 100
        with patch.object(rc.subprocess, 'run') as call, self.assertRaises(rc.ReleaseError):
            self.reader.json('releases')
        call.assert_not_called()
        self.reader.calls = 0
        self.reader.remaining = 1
        with patch.object(rc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, wire(200, b'[]'), b'')):
            with self.assertRaisesRegex(rc.ReleaseError, 'response limit'):
                self.reader.json('releases')

    def test_read_error_never_returns_partial_data_or_retries(self):
        for raw in (b'HTTP/2.0 200 OK\r\n', wire(503, b'[]'), wire(200, b'[{"id":')):
            with self.subTest(raw=raw), patch.object(rc.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 1, raw, b'EOF')) as call:
                with self.assertRaises(rc.ReleaseError):
                    self.reader.pages('releases')
                call.assert_called_once()


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.gh = Mock(repo=REPO)
        self.payload = {'tag_name': 'v1.2.3-beta.1', 'target_commitish': SHA, 'name': 'v1.2.3-beta.1',
                        'body': 'notes\n<!-- fresh per-call nonce -->', 'draft': True,
                        'prerelease': True, 'make_latest': 'false'}
        self.release = {**self.payload, 'id': 42, 'published_at': None,
                        'url': f'https://api.github.com/repos/{REPO}/releases/42',
                        'created_at': '2000-01-01T00:00:00Z', 'html_url': 'untagged-valid-draft',
                        'author': {'id': 123}, 'assets': []}
        self.ref = {'ref': 'refs/tags/v1.2.3-beta.1', 'object': {'type': 'commit', 'sha': SHA}}
        self.reader = Mock()
        self.reader.pages.side_effect = lambda path: [copy.deepcopy(self.release)] if path == 'releases' else []
        self.reader.json.side_effect = lambda path: copy.deepcopy(self.ref if path.startswith('git/') else self.release)
        self.confirm = Mock()

    def reconcile(self):
        with patch.object(rc, 'CreateReadback', return_value=self.reader):
            return rc._reconcile_created_release(self.gh, self.payload, self.confirm)

    def test_exact_empty_draft_same_call_only(self):
        self.assertEqual(self.reconcile()['id'], 42)
        self.assertEqual(self.confirm.call_count, 2)
        self.gh.assert_not_called()

    def test_identity_and_state_mismatch(self):
        for key, value in [('body', 'older nonce'), ('target_commitish', 'b' * 40),
                           ('draft', False), ('prerelease', False), ('published_at', 'now'),
                           ('url', 'https://evil.test/release/42'), ('name', 'wrong'),
                           ('tag_name', 'v9.9.9'), ('assets', [{'id': 90}]), ('author', None)]:
            with self.subTest(key=key):
                before = copy.deepcopy(self.release)
                self.release[key] = value
                with self.assertRaises(rc.ReleaseError):
                    self.reconcile()
                self.release = before

    def test_no_duplicate_or_nonempty_draft(self):
        for mode in ('missing', 'duplicate', 'assets', 'changing'):
            with self.subTest(mode=mode):
                self.reader.pages.side_effect = {
                    'missing': [[], []], 'duplicate': [[self.release] * 2] * 2,
                    'assets': [[self.release], [self.release], [{'id': 90}]],
                    'changing': [[self.release], []],
                }[mode]
                with self.assertRaises(rc.ReleaseError):
                    self.reconcile()

    def test_changed_tag_or_callback_failure_prevents_return(self):
        self.ref['object']['sha'] = 'b' * 40
        with self.assertRaisesRegex(rc.ReleaseError, 'tag source'):
            self.reconcile()
        self.confirm.side_effect = rc.ReleaseError('ledger changed')
        with self.assertRaisesRegex(rc.ReleaseError, 'ledger changed'):
            self.reconcile()


class ConfirmationTests(unittest.TestCase):
    def test_fresh_nonce_strict_current_attempt_and_consumed_ledger(self):
        run = {'id': 99, 'run_attempt': 1, 'head_sha': SHA, 'path': rc.WORKFLOW, 'event': 'push'}
        source = {'object': {'sha': SHA}}
        consumed = b'{"consumed":true}'
        body = lifecycle.create_marker(REPO, run, 'a' * 64, lifecycle.secrets.token_hex(32))
        body2 = lifecycle.create_marker(REPO, run, 'a' * 64, lifecycle.secrets.token_hex(32))
        confirm = lifecycle.create_confirmation(run, ('git/ref/heads/main', source), consumed)
        self.assertNotEqual(body, body2)
        self.assertIn('"attempt":1', body)
        current = {**run, 'status': 'in_progress', 'conclusion': None}
        ledger = {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(consumed).decode()}
        reader = Mock()
        reader.json.side_effect = lambda path: current if path.startswith('actions/') else source if path.startswith('git/') else ledger
        confirm(reader)
        for key, value in [('run_attempt', 2), ('status', 'completed'), ('head_sha', 'b' * 40)]:
            before = copy.deepcopy(current)
            current[key] = value
            with self.assertRaises(rc.ReleaseError):
                confirm(reader)
            current = before
        ledger['content'] = base64.b64encode(b'changed').decode()
        with self.assertRaisesRegex(rc.ReleaseError, 'ledger changed'):
            confirm(reader)


if __name__ == '__main__':
    unittest.main()

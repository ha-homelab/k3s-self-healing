"""Core PRs contain only verified image pins and merge an approved exact head."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core_updates'))
import proposals
from model import Store

BASE = 'a' * 40
HEAD = 'b' * 40
IMAGE = 'ghcr.io/home-assistant/home-assistant:2026.12.0@sha256:' + 'c' * 64


class FakeGitHub:
    repository = 'example/core-infra'

    def __init__(self):
        self.files = {BASE: {path: (ROOT / path).read_text() for path in (proposals.VALUES, proposals.BACKUP)}}
        self.refs = {'main': BASE}
        self.prs = []
        self.merges = []
        self.count = 1
        self.fail_link = False

    def read(self, commit, path):
        return self.files[commit][path]

    def commit(self, parent, files, message):
        self.count += 1
        sha = f'{self.count:040x}'
        self.files[sha] = self.files[parent] | files
        return sha

    def api(self, path, data=None, method=None):
        if path.startswith('git/ref/heads/'):
            branch = path.removeprefix('git/ref/heads/')
            if branch not in self.refs:
                raise proposals.GitHubError(404)
            return {'object': {'sha': self.refs[branch]}}
        if path.startswith('pulls?'):
            return deepcopy(self.prs)
        if path == 'git/refs':
            self.refs[data['ref'].removeprefix('refs/heads/')] = data['sha']
            return {}
        if path == 'pulls':
            pr = {'number': 17, 'html_url': 'https://github.com/example/core-infra/pull/17',
                  'state': 'open', 'draft': False,
                  'head': {'sha': self.refs[data['head']], 'ref': data['head'], 'repo': {'full_name': self.repository}},
                  'base': {'ref': 'main', 'repo': {'full_name': self.repository}}}
            self.prs.append(pr)
            return deepcopy(pr)
        if path.startswith('git/refs/heads/'):
            if self.fail_link:
                self.fail_link = False
                raise proposals.GitHubError(503)
            branch = path.removeprefix('git/refs/heads/')
            self.refs[branch] = data['sha']
            self.prs[0]['head']['sha'] = data['sha']
            return {}
        if path.startswith('compare/'):
            base, head = path.removeprefix('compare/').split('...')
            changed = [p for p in self.files[head] if self.files[head].get(p) != self.files[base].get(p)]
            return {'status': 'ahead', 'total_commits': 2, 'files': [{'filename': p} for p in changed]}
        if path == 'pulls/17':
            return deepcopy(self.prs[0])
        if path == 'pulls/17/merge':
            self.merges.append(data)
            return {'merged': True, 'sha': 'f' * 40}
        raise AssertionError(path)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.release = patch('proposals.release', return_value=('2026.12.0', 'https://github.com/home-assistant/core/releases/tag/2026.12.0'))
        self.digest = patch('proposals.image_for', return_value=IMAGE)
        self.release.start()
        self.digest.start()
        self.addCleanup(self.release.stop)
        self.addCleanup(self.digest.stop)

    def test_real_git_exact_content_and_deleted_merged_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_COMMITTER_NAME='Test',
                       GIT_AUTHOR_EMAIL='test@example.test', GIT_COMMITTER_EMAIL='test@example.test')
            def git(*args):
                result = subprocess.run(['git', *args], cwd=path, env=env,
                                        text=True, capture_output=True)
                if result.returncode:
                    raise RuntimeError('Git command rejected')
                return result.stdout.strip()
            git('init', '-b', 'main')
            for name in (proposals.VALUES, proposals.BACKUP):
                target = path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((ROOT / name).read_text())
            git('add', '.'); git('commit', '-m', 'Base')
            base = git('rev-parse', 'HEAD')
            files = proposals.render_pins((path / proposals.VALUES).read_text(),
                                         (path / proposals.BACKUP).read_text(), IMAGE)
            files[proposals.MANIFEST] = json.dumps({'schema': 1, 'version': '2026.12.0',
                'image': IMAGE, 'base_commit': base, 'pull_request': 17})
            for name, content in files.items():
                (path / name).parent.mkdir(parents=True, exist_ok=True)
                (path / name).write_text(content)
            git('add', '.'); git('commit', '-m', 'Proposal')
            head = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/main', head)
            source = Mock(path=path, env=env,
                          settings={'repository': 'git@github.com:example/core-infra.git'})
            for branch_deleted in (False, True):
                def adapted(*args):
                    if args[0] == 'fetch':
                        refspec = args[-1]
                        if refspec.startswith('+refs/heads/') and branch_deleted:
                            raise RuntimeError('Branch deleted')
                        if ':' in refspec:
                            git('update-ref', refspec.split(':', 1)[1], head)
                        return ''
                    return git(*args)
                source.git.side_effect = adapted
                result = proposals.read_proposal(source, IMAGE)
                self.assertEqual(result['proposal_sha'], head)
                self.assertEqual(result['pull_request_number'], 17)
                self.assertEqual(proposals.wait_merge(source, result | {'image': IMAGE}, 1), head)

    def test_discovery_creates_one_pr_and_both_immutable_pins(self):
        result = proposals.propose(self.api)
        self.assertEqual(result['status'], 'created')
        final = self.api.refs['ha-core-updates/2026.12.0']
        data = json.loads(self.api.read(final, proposals.MANIFEST))
        self.assertEqual(data['pull_request'], 17)
        expected = proposals.render_pins(self.api.read(BASE, proposals.VALUES), self.api.read(BASE, proposals.BACKUP), IMAGE)
        self.assertEqual(self.api.files[final][proposals.VALUES], expected[proposals.VALUES])
        self.assertEqual(self.api.files[final][proposals.BACKUP], expected[proposals.BACKUP])
        self.assertEqual(proposals.propose(self.api)['status'], 'existing')
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(self.api.merges, [])

    def test_partial_pr_link_is_repaired_without_duplicate_pr(self):
        self.api.fail_link = True
        with self.assertRaises(proposals.GitHubError):
            proposals.propose(self.api)
        result = proposals.propose(self.api)
        self.assertEqual(result['status'], 'created')
        self.assertEqual(len(self.api.prs), 1)
        data = json.loads(self.api.read(result['head'], proposals.MANIFEST))
        self.assertEqual(data['pull_request'], 17)

    def test_closed_pr_is_not_reopened(self):
        proposals.propose(self.api)
        self.api.prs[0]['state'] = 'closed'
        self.assertEqual(proposals.propose(self.api)['state'], 'closed')
        self.assertEqual(len(self.api.prs), 1)

    def test_stable_older_than_installed_beta_does_not_create_pr(self):
        with patch('proposals.release', return_value=('2020.1.0', 'url')):
            self.assertEqual(proposals.propose(self.api)['status'], 'up_to_date')
        self.assertEqual(self.api.prs, [])

    def test_merge_requires_the_exact_open_pr_head(self):
        head = proposals.propose(self.api)['head']
        result = proposals.merge_request(self.api, 'refs/tags/ha-core-install/' + 'a' * 32, head)
        self.assertEqual(result['status'], 'merged')
        self.assertEqual(self.api.merges, [{'sha': head, 'merge_method': 'merge'}])
        for changes in ({'state': 'closed'}, {'draft': True}, {'head': {'sha': HEAD}}):
            before = deepcopy(self.api.prs[0])
            self.api.prs[0].update(changes)
            with self.assertRaises(ValueError):
                proposals.merge_request(self.api, 'refs/tags/ha-core-install/' + 'a' * 32, head)
            self.api.prs[0] = before
        self.assertEqual(len(self.api.merges), 1)

    def test_foreign_file_or_non_pin_change_blocks_merge(self):
        head = proposals.propose(self.api)['head']
        original = deepcopy(self.api.files[head])
        self.api.files[head]['.github/workflows/unsafe.yml'] = 'changed'
        with self.assertRaises(ValueError):
            proposals.merge_request(self.api, 'refs/tags/ha-core-install/' + 'a' * 32, head)
        self.api.files[head] = original
        self.api.files[head][proposals.VALUES] += '\nextraSetting: true\n'
        with self.assertRaises(ValueError):
            proposals.merge_request(self.api, 'refs/tags/ha-core-install/' + 'a' * 32, head)
        self.assertEqual(self.api.merges, [])

    def test_new_proposal_head_invalidates_same_version_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'status.json')
            first = store.offer('2026.11.0', '2026.12.0', IMAGE, 'base', 'url', proposal={'proposal_sha': BASE})
            second = store.offer('2026.11.0', '2026.12.0', IMAGE, 'base', 'url', proposal={'proposal_sha': HEAD})
            self.assertNotEqual(first['candidate_id'], second['candidate_id'])
            with self.assertRaises(RuntimeError):
                store.approve({'action': 'install', 'target_version': '2026.12.0', 'candidate_id': first['candidate_id']})

    def test_unmerged_pr_never_confirms_source(self):
        source = Mock()
        source.git.side_effect = ['', RuntimeError('not ancestor')]
        with patch('proposals.time.monotonic', side_effect=[0, 0, 2]), patch('proposals.time.sleep'):
            with self.assertRaises(RuntimeError):
                proposals.wait_merge(source, {'proposal_sha': HEAD}, 1)


if __name__ == '__main__':
    unittest.main()

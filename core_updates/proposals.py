"""Prepare Core pin PRs in Actions; merge only an explicitly requested PR head.

GitHub credentials stay in Actions. The in-cluster worker uses its existing SSH
deploy key to request a merge via an immutable tag, then verifies main by Git.
Neither Actions mode has Kubernetes credentials or performs a deployment.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request

import yaml

from model import IMAGE, version_tuple
from runtime import image_for, release, urlopen

VALUES = 'helm/values-pajikos.yaml'
BACKUP = 'native_backup/cronjob.yaml'
MANIFEST = 'core_updates/candidate.json'
BRANCH_PREFIX = 'ha-core-updates/'
REQUEST_PREFIX = 'ha-core-install/'
SHA = re.compile(r'[0-9a-f]{40}\Z')


def render_pins(values, backup, image):
    from worker import image_tag, pin_backup_manifest
    parsed = yaml.safe_load(values)
    if parsed['image']['repository'] != 'ghcr.io/home-assistant/home-assistant':
        raise ValueError('Unexpected Core image repository')
    block = re.search(r'(?ms)^image:\n(?:(?:[ \t]+[^\n]*|)\n)+', values)
    if not block:
        raise ValueError('Core image values are missing')
    replacement, count = re.subn(r'(?m)^  tag:.*$', '  tag: ' + json.dumps(image_tag(image)), block.group())
    if count != 1:
        raise ValueError('Ambiguous Core image tag')
    return {VALUES: values[:block.start()] + replacement + values[block.end():],
            BACKUP: pin_backup_manifest(backup, image)}


def validate_manifest(data, image=None, ready=True):
    if not isinstance(data, dict) or set(data) != {'schema', 'version', 'image', 'base_commit', 'pull_request'}:
        raise ValueError('Unexpected Core proposal manifest')
    if data['schema'] != 1 or not SHA.fullmatch(data['base_commit'] or ''):
        raise ValueError('Invalid Core proposal base')
    version_tuple(data['version'])
    match = IMAGE.fullmatch(data['image'] or '')
    if not match or match.group(1) != data['version'] or 'b' in data['version']:
        raise ValueError('Core proposals require an immutable stable image')
    if image is not None and data['image'] != image:
        raise ValueError('Core proposal image differs from the offered image')
    number = data['pull_request']
    if type(number) is not int or number < (1 if ready else 0):
        raise ValueError('Core proposal has no pull request')
    return data


def verify_content(data, image, changed, base_reader, head_reader, ready=True):
    validate_manifest(data, image, ready=ready)
    if set(changed) != {VALUES, BACKUP, MANIFEST}:
        raise ValueError('Core proposal changes files outside the two image pins and manifest')
    expected = render_pins(base_reader(VALUES), base_reader(BACKUP), image)
    if any(head_reader(path) != content for path, content in expected.items()):
        raise ValueError('Core proposal contains changes other than the exact image pins')


def read_proposal(source, image):
    match = IMAGE.fullmatch(image or '')
    if not match:
        raise ValueError('Invalid offered image')
    branch = BRANCH_PREFIX + match.group(1)
    ref = 'refs/remotes/origin/' + branch
    try:
        source.git('fetch', '--no-tags', 'origin', '+refs/heads/' + branch + ':' + ref)
        head = source.git('rev-parse', ref)
    except RuntimeError:
        # GitHub may have deleted a successfully merged proposal branch. Keep
        # the original PR head identity, not a later unrelated main commit.
        source.git('fetch', '--no-tags', 'origin', 'main')
        merged = validate_manifest(json.loads(source.git('show', 'origin/main:' + MANIFEST)), image)
        ref = 'refs/remotes/origin/core-proposal-merged'
        source.git('fetch', '--no-tags', 'origin',
                   f'+refs/pull/{merged["pull_request"]}/head:{ref}')
        head = source.git('rev-parse', ref)
        source.git('merge-base', '--is-ancestor', head, 'origin/main')
    data = validate_manifest(json.loads(source.git('show', head + ':' + MANIFEST)), image)
    base = data['base_commit']
    source.git('merge-base', '--is-ancestor', base, head)
    # Source.git strips command output; preserve final newlines for exact comparison.
    from runtime import command
    def read(commit, path):
        return command(['git', 'show', commit + ':' + path], cwd=source.path, env=source.env)
    verify_content(data, image, source.git('diff', '--name-only', base, head).splitlines(),
                   lambda path: read(base, path), lambda path: read(head, path))
    repo = source.settings['repository'].removeprefix('git@github.com:').removesuffix('.git')
    return {'proposal_sha': head, 'pull_request_number': data['pull_request'],
            'pull_request_url': f'https://github.com/{repo}/pull/{data["pull_request"]}'}


def wait_merge(source, approval, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        source.git('fetch', '--no-tags', 'origin', 'main')
        try:
            source.git('merge-base', '--is-ancestor', approval['proposal_sha'], 'origin/main')
        except RuntimeError:
            time.sleep(10)
            continue
        main = source.git('rev-parse', 'origin/main')
        data = validate_manifest(json.loads(source.git('show', main + ':' + MANIFEST)), approval['image'])
        if data['pull_request'] != approval['pull_request_number']:
            raise RuntimeError('Merged Core proposal identity changed')
        # A later writer cannot silently replace either merged runtime image pin.
        for path in (VALUES, BACKUP):
            if source.git('show', main + ':' + path) != source.git('show', approval['proposal_sha'] + ':' + path):
                raise RuntimeError('Main image pins changed after the approved PR merge')
        return main
    raise RuntimeError('Core PR merge was not confirmed; inspect the merge-request workflow')


class GitHubError(RuntimeError):
    def __init__(self, status):
        super().__init__(f'GitHub request rejected: HTTP {status}')
        self.status = status


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', repository or '') or not token:
            raise ValueError('Explicit repository and Actions token are required')
        self.repository, self.token = repository, token

    def api(self, path, data=None, method=None):
        request = Request('https://api.github.com/repos/' + self.repository + '/' + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method, headers={'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ha-core-proposals'})
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            # API response bodies and request headers must never enter logs.
            raise GitHubError(error.code) from None

    def read(self, commit, path):
        data = self.api('contents/' + path + '?ref=' + quote(commit, safe=''))
        if data.get('encoding') != 'base64' or data.get('type') != 'file':
            raise ValueError('Proposal source must be a regular file')
        return base64.b64decode(data['content']).decode()

    def commit(self, parent, files, message):
        base_tree = self.api('git/commits/' + parent)['tree']['sha']
        tree = self.api('git/trees', {'base_tree': base_tree, 'tree': [
            {'path': path, 'mode': '100644', 'type': 'blob', 'content': content}
            for path, content in files.items()]})['sha']
        return self.api('git/commits', {'message': message, 'tree': tree, 'parents': [parent]})['sha']


def propose(api):
    version, release_url = release()  # Stable-only policy, independent of installed beta.
    main = api.api('git/ref/heads/main')['object']['sha']
    values, backup = api.read(main, VALUES), api.read(main, BACKUP)
    current = str(yaml.safe_load(values)['image']['tag']).split('@', 1)[0]
    if version_tuple(version) <= version_tuple(current):
        return {'status': 'up_to_date', 'current': current, 'stable': version}
    branch = BRANCH_PREFIX + version
    owner = api.repository.split('/')[0]
    # Closed PRs are an explicit decision; do not recreate them on every timer.
    prs = api.api('pulls?' + urlencode({'state': 'all', 'head': owner + ':' + branch, 'base': 'main', 'per_page': 100}))
    existing = next((pr for pr in prs if pr['head']['ref'] == branch), None)
    if existing and existing['state'] != 'open':
        return {'status': 'existing', 'pull_request': existing['html_url'], 'state': existing['state']}
    image = image_for(version)
    try:
        head = api.api('git/ref/heads/' + branch)['object']['sha']
    except GitHubError as error:
        if error.status != 404:
            raise
        head = None
    if head:
        data = validate_manifest(json.loads(api.read(head, MANIFEST)), image, ready=False)
        comparison = api.api('compare/' + data['base_commit'] + '...' + head)
        verify_content(data, image, [f['filename'] for f in comparison.get('files', [])],
                       lambda path: api.read(data['base_commit'], path), lambda path: api.read(head, path), ready=False)
        if data['pull_request']:
            if not existing or data['pull_request'] != existing['number']:
                raise ValueError('Existing Core proposal identity is inconsistent')
            return {'status': 'existing', 'pull_request': existing['html_url'], 'state': 'open'}
    else:
        data = {'schema': 1, 'version': version, 'image': image, 'base_commit': main, 'pull_request': 0}
        files = render_pins(values, backup, image)
        files[MANIFEST] = json.dumps(data, indent=2) + '\n'
        head = api.commit(main, files, 'Propose Home Assistant ' + version)
        api.api('git/refs', {'ref': 'refs/heads/' + branch, 'sha': head})
    pr = existing or api.api('pulls', {'title': 'Update Home Assistant Core to ' + version, 'head': branch, 'base': 'main',
        'body': 'Automated stable Core proposal. Both runtime image pins use the same immutable registry digest.\n\n'
                'Home Assistant Install authorizes a fresh configuration backup and candidate preflight before '
                'requesting this PR merge. Merging this PR alone does not deploy or restart Home Assistant.\n\n'
                '[Upstream release notes](' + release_url + ')'})
    data['pull_request'] = pr['number']
    final = api.commit(head, {MANIFEST: json.dumps(data, indent=2) + '\n'}, 'Link Core proposal to its pull request')
    api.api('git/refs/heads/' + branch, {'sha': final, 'force': False}, method='PATCH')
    return {'status': 'created', 'version': version, 'pull_request': pr['html_url'], 'head': final}


def merge_request(api, ref, head):
    if not re.fullmatch(r'refs/tags/ha-core-install/[0-9a-f]{32}', ref or '') or not SHA.fullmatch(head or ''):
        raise ValueError('A worker-generated immutable merge request is required')
    data = validate_manifest(json.loads(api.read(head, MANIFEST)))
    pr = api.api('pulls/' + str(data['pull_request']))
    if pr.get('merged') and pr['head']['sha'] == head:
        # An operator can merge the prepared PR before pressing Install. The
        # worker still verifies ancestry and both pins before any deployment.
        return {'status': 'already_merged', 'pull_request': data['pull_request']}
    if (pr['state'] != 'open' or pr.get('draft') or pr['head']['sha'] != head
            or pr['head']['repo']['full_name'] != api.repository
            or pr['base']['repo']['full_name'] != api.repository or pr['base']['ref'] != 'main'
            or pr['head']['ref'] != BRANCH_PREFIX + data['version']):
        raise ValueError('Core PR was closed, changed, or is not the expected same-repository proposal')
    comparison = api.api('compare/' + data['base_commit'] + '...' + head)
    if comparison.get('status') != 'ahead' or comparison.get('total_commits', 0) > 4:
        raise ValueError('Unexpected proposal ancestry')
    verify_content(data, data['image'], [f['filename'] for f in comparison.get('files', [])],
                   lambda path: api.read(data['base_commit'], path), lambda path: api.read(head, path))
    result = api.api('pulls/' + str(data['pull_request']) + '/merge',
                    {'sha': head, 'merge_method': 'merge'}, method='PUT')
    if result.get('merged') is not True:
        raise RuntimeError('GitHub refused the ordinary PR merge')
    return {'status': 'merged', 'pull_request': data['pull_request'], 'sha': result['sha']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('propose', 'merge'))
    args = parser.parse_args()
    api = GitHub(os.environ.get('GITHUB_REPOSITORY'), os.environ.get('GITHUB_TOKEN'))
    result = propose(api) if args.action == 'propose' else merge_request(
        api, os.environ.get('GITHUB_REF'), os.environ.get('GITHUB_SHA'))
    print(json.dumps(result))


if __name__ == '__main__':
    main()

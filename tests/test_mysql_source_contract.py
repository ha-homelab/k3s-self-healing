"""Bind optional capture sources to the real source/archive/security gates."""
import copy
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'disaster_recovery/mysql_backup'
GO_ACTION = 'actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e'
GO_ENV = {'GOENV': 'off', 'GOAUTH': 'off', 'GOTOOLCHAIN': 'local',
          'GOPROXY': 'https://proxy.golang.org', 'GOSUMDB': 'sum.golang.org'}


def require_capture_checks(validation, codeql):
    source = validation['jobs']['source']
    assert 'if' not in source
    setups = [s for s in source['steps'] if s.get('uses') == GO_ACTION]
    assert len(setups) == 1 and 'if' not in setups[0]
    assert setups[0]['with']['go-version'] == '1.27.4'
    assert setups[0]['with']['cache-dependency-path'] == MODULE + '/go.sum'
    tests = [s for s in source['steps'] if s.get('run', '').startswith('go test ')]
    assert len(tests) == 1 and 'if' not in tests[0]
    assert tests[0]['working-directory'] == MODULE
    assert tests[0]['run'] == 'go test -mod=readonly -count=1 -timeout=120s ./...'
    assert tests[0]['env'] == GO_ENV
    analyze = codeql['jobs']['analyze']
    assert 'go' in analyze['strategy']['matrix']['language']
    initializers = [s for s in analyze['steps'] if s.get('uses', '').startswith('github/codeql-action/init@')]
    assert len(initializers) == 1
    assert initializers[0]['with']['build-mode'] == "${{ matrix.language == 'go' && 'manual' || '' }}"
    setups = [s for s in analyze['steps'] if s.get('uses') == GO_ACTION]
    assert len(setups) == 1 and setups[0]['if'] == "matrix.language == 'go'"
    assert setups[0]['with']['go-version'] == '1.27.4'
    builds = [s for s in analyze['steps'] if s.get('run', '').startswith('go build ')]
    assert len(builds) == 1 and builds[0]['if'] == "matrix.language == 'go'"
    assert builds[0]['working-directory'] == MODULE
    assert builds[0]['run'] == 'go build -mod=readonly ./...'
    assert builds[0]['env'] == GO_ENV | {'CODEQL_EXTRACTOR_GO_BUILD_TRACING': 'on'}
    assert analyze['steps'].index(initializers[0]) < analyze['steps'].index(builds[0])
    analyzers = [s for s in analyze['steps'] if s.get('uses', '').startswith('github/codeql-action/analyze@')]
    assert len(analyzers) == 1 and analyze['steps'].index(builds[0]) < analyze['steps'].index(analyzers[0])


class MySQLSourceContractTests(unittest.TestCase):
    def setUp(self):
        self.validation = yaml.safe_load((ROOT / '.github/workflows/validate.yml').read_text())
        self.codeql = yaml.safe_load((ROOT / '.github/workflows/codeql.yml').read_text())

    def test_real_source_and_security_workflows_require_capture_checks(self):
        require_capture_checks(self.validation, self.codeql)

    def test_missing_skipped_unlocked_or_wrong_module_tests_are_refused(self):
        for mutation in ('missing', 'skipped', 'unlocked', 'wrong_module', 'wrong_toolchain'):
            validation = copy.deepcopy(self.validation)
            steps = validation['jobs']['source']['steps']
            test = next(s for s in steps if s.get('run', '').startswith('go test '))
            if mutation == 'missing':
                steps.remove(test)
            elif mutation == 'skipped':
                test['if'] = 'false'
            elif mutation == 'unlocked':
                test['run'] = 'go test ./...'
            elif mutation == 'wrong_module':
                test['working-directory'] = '.'
            else:
                next(s for s in steps if s.get('uses') == GO_ACTION)['with']['go-version'] = '1.25.0'
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                require_capture_checks(validation, self.codeql)

    def test_missing_or_untraced_go_analysis_is_refused(self):
        for mutation in ('language', 'build', 'mode', 'tracing'):
            codeql = copy.deepcopy(self.codeql)
            analyze = codeql['jobs']['analyze']
            build = next(s for s in analyze['steps'] if s.get('run', '').startswith('go build '))
            if mutation == 'language':
                analyze['strategy']['matrix']['language'].remove('go')
            elif mutation == 'build':
                analyze['steps'].remove(build)
            elif mutation == 'mode':
                next(s for s in analyze['steps'] if s.get('uses', '').startswith('github/codeql-action/init@'))['with']['build-mode'] = 'none'
            else:
                build['env'].pop('CODEQL_EXTRACTOR_GO_BUILD_TRACING')
            with self.subTest(mutation=mutation), self.assertRaises(AssertionError):
                require_capture_checks(self.validation, codeql)

    def test_exact_capture_source_set_is_in_the_publication_inventory(self):
        files = json.loads((ROOT / 'publication.json').read_text())['files']
        expected = {MODULE + '/' + name for name in ('README.md', 'go.mod', 'go.sum', 'main.go', 'main_test.go')}
        expected |= {'disaster_recovery/mysql_capture.py', 'tests/test_k3s_mysql_capture.py',
                     'tests/test_k3s_mysql_runner.py', 'tests/test_mysql_source_contract.py'}
        self.assertTrue(expected <= set(files))
        for name in expected:
            self.assertTrue((ROOT / name).is_file())
            self.assertFalse((ROOT / name).is_symlink())
        self.assertEqual((ROOT / MODULE / 'go.mod').read_text().splitlines()[0],
                         'module github.com/ha-homelab/k3s-self-healing/disaster_recovery/mysql_backup')

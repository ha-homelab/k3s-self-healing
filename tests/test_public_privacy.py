"""Exercise leak detection using generated synthetic identifiers."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from privacy_scan import findings


class PrivacyTests(unittest.TestCase):
    def test_phone_and_hardware_address_are_rejected_without_echo(self):
        values = ['+' + '1' + '2025550199', ':'.join(['aa', 'bb', 'cc', 'dd', 'ee', 'ff'])]
        for value in values:
            result = findings('fixture', value)
            self.assertTrue(result)
            self.assertNotIn(value, str(result))

    def test_device_model_and_github_authorship_are_allowed(self):
        self.assertEqual(findings('fixture', 'iPhone Air; @4alvit; git@github.com:ha-homelab/k3s-self-healing.git'), [])

    def test_reserved_email_domain_does_not_allow_other_domains(self):
        self.assertFalse(findings('fixture', 'email@example.test'))
        self.assertTrue(findings('fixture', 'email@' + 'operator.test'))

    def test_documentation_address_allowed_private_endpoint_rejected(self):
        self.assertFalse(findings('fixture', 'http://192.0.2.10:8123'))
        self.assertTrue(findings('fixture', 'http://' + '.'.join(['192', '168', '77', '42'])))

    def test_public_policy_contact_is_allowed_only_in_policy_files(self):
        contact = 'alvit.work' + '@gmail.com'
        for name in ('SECURITY.md', 'CODE_OF_CONDUCT.md'):
            self.assertFalse(findings(name, contact))
        self.assertTrue(findings('runtime.json', contact))
        self.assertTrue(findings('SECURITY.md', 'someone' + '@gmail.com'))

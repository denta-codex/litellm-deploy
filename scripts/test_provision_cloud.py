"""Reject identity mismatch and avoid replacing an existing Love credential."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import provision_cloud as cloud


class CloudProvisionTests(unittest.TestCase):
    def setUp(self):
        account = type('Account', (), {'pw_dir': '/home/agent', 'pw_uid': 1000})()
        self.enterContext(patch('provision_cloud.pwd.getpwnam', return_value=account))
        self.enterContext(patch('provision_cloud.os.geteuid', return_value=1000))
        self.enterContext(patch('provision_cloud.socket.gethostname', return_value='grace'))
        self.identity = 'love\nagent\nagent:x:2002:2003::' + cloud.DESTINATION + ':/bin/bash\n'

    def test_wrong_destination_stops_before_secret_read(self):
        with patch.object(cloud, 'command', return_value=self.identity.replace('love', 'other')) as command:
            with self.assertRaisesRegex(ValueError, 'identity differs'):
                cloud.provision('bedrock')
        self.assertEqual(command.call_count, 1)

    def test_existing_credential_is_checked_without_replacement(self):
        with patch.object(cloud, 'command', side_effect=[self.identity, 'private-value']) as command, \
                patch.object(cloud, 'aws_values') as validate, \
                patch('provision_cloud.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            report = cloud.provision('bedrock')
        self.assertFalse(report['changed'])
        validate.assert_called_once_with('private-value')
        self.assertEqual(command.call_count, 2)
        self.assertNotIn('private-value', json.dumps(report))

    def test_creation_passes_secret_only_on_stdin_and_rechecks_storage(self):
        with patch.object(cloud, 'command', side_effect=[self.identity, 'private-value', b'', 'private-value']) as command, \
                patch.object(cloud, 'aws_values'), \
                patch('provision_cloud.subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
            report = cloud.provision('bedrock')
        self.assertTrue(report['changed'])
        encryption = command.call_args_list[2]
        self.assertEqual(encryption.kwargs['input'], b'private-value')
        self.assertNotIn('private-value', ' '.join(encryption.args[0]))
        self.assertIn('ln ', encryption.args[0][-1])
        self.assertIn('trap ', encryption.args[0][-1])

    def test_command_failure_redacts_private_output(self):
        with patch('provision_cloud.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'private-value', b'private-value')):
            with self.assertRaises(ValueError) as caught:
                cloud.command(['fixture'])
        self.assertNotIn('private-value', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

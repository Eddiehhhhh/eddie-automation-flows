import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('checkout', Path(__file__).parents[1] / 'scripts/cloud_private_checkout.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class PrivateCheckoutTests(unittest.TestCase):
    def test_auth_is_ephemeral_and_command_output_is_captured(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(c.os.environ, {'PRIVATE_VAULT_TOKEN': 'fixture-secret'}), \
                mock.patch.object(c.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"private":true}')), \
                mock.patch.object(c.subprocess, 'run') as run:
            c.checkout(Path(folder)/'vault')
            argv = run.call_args.args[0]
            options = run.call_args.kwargs
            self.assertNotIn('fixture-secret', ' '.join(argv))
            self.assertTrue(options['capture_output'])
            self.assertNotIn('PRIVATE_VAULT_TOKEN', options['env'])
            self.assertEqual(options['env']['GIT_CONFIG_COUNT'], '1')

    def test_failure_never_echoes_token_head_or_repository(self):
        error = subprocess.CalledProcessError(1, ['secret-command'],
            output='PRIVATE_HEAD_AND_REPOSITORY', stderr='fixture-secret')
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['checkout', '--destination', 'unused']), \
                mock.patch.object(c, 'checkout', side_effect=error), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(c.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), 'Private checkout failed; details suppressed.\n')

    def test_public_repository_is_rejected_without_clone(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(c.os.environ, {'PRIVATE_VAULT_TOKEN': 'fixture-secret'}), \
                mock.patch.object(c.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"private":false}')), \
                mock.patch.object(c.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                c.checkout(Path(folder)/'vault')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

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

    def test_existing_checkout_identity_visibility_and_push_url_are_required(self):
        from types import SimpleNamespace
        private = 'https://github.com/Eddiehhhhh/eddie-llm-wiki.git'
        public = 'https://github.com/Eddiehhhhh/eddie-automation-flows.git'
        for origins, metadata, error, good in [
            ([private, private], {'private':True,'full_name':c.REPOSITORY}, None, True),
            ([public, public], {}, None, False),
            ([private, public], {}, None, False),
            ([private, private + '\n' + public], {}, None, False),
            ([private, private + '\n' + private], {}, None, False),
            ([private + '\n' + private, private], {}, None, False),
            (['', private], {}, None, False),
            ([private, private], {'private':False,'full_name':c.REPOSITORY}, None, False),
            ([private, private], {'private':True,'full_name':'other/repo'}, None, False),
            ([private, private], {}, TimeoutError('PRIVATE_SENTINEL'), False),
        ]:
            with self.subTest(origins=origins, metadata=metadata), tempfile.TemporaryDirectory() as tmp, mock.patch.object(c.subprocess,'run',side_effect=[SimpleNamespace(returncode=0,stdout=x) for x in origins]) as run, mock.patch.object(c,'verify_private_repository', return_value=metadata, side_effect=error):
                if good:
                    c.verify_private_checkout(Path(tmp), 'synthetic')
                else:
                    with self.assertRaises(ValueError) as held:
                        c.verify_private_checkout(Path(tmp), 'synthetic')
                    self.assertEqual(str(held.exception), 'Private checkout verification failed')
                self.assertTrue(all('--all' in call.args[0] for call in run.call_args_list))
                self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

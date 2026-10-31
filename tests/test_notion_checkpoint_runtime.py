"""Packaged helper, bounded process group and public diagnostic contracts."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sync_wiki_to_notion as m
REAL_RUN=subprocess.run


class CheckpointRuntimeTests(unittest.TestCase):
    def test_checkpoint_uses_an_actual_packaged_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'.github/state/notion-wiki-mirror-state.json'
            calls=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=0,stdout='abc\n'),SimpleNamespace(returncode=0,stdout='abc\trefs/heads/main\n')]
            def run_help_only(command):
                self.assertEqual(command[0],sys.executable)
                self.assertTrue(Path(command[1]).is_file())
                self.assertEqual(Path(command[1]).name,'commit_private_vault.py')
                self.assertNotIn('run_bounded.py',' '.join(command))
                result=REAL_RUN(command[:2]+['--help'],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                return 0
            with mock.patch.object(m,'ROOT',root),mock.patch.object(m,'STATE_PATH',state),mock.patch.object(m,'verify_private_checkout'),mock.patch.object(m.subprocess,'run',side_effect=calls),mock.patch.object(m,'_run_checkpoint',side_effect=run_help_only):
                m.persist_private_state({'entries':{}})

    def test_missing_packaged_helper_fails_before_state_write(self):
        with mock.patch.object(m.Path,'is_file',return_value=False),mock.patch.object(m,'write_json') as write:
            with self.assertRaises(m.MirrorHeld) as held:m.persist_private_state({'PRIVATE_SENTINEL':'value'})
            self.assertEqual(held.exception.code,'checkpoint_helper_unavailable')
            write.assert_not_called()

    def test_process_group_output_is_captured_and_timeout_kills_owned_group(self):
        process=mock.Mock(pid=987654,returncode=0)
        with mock.patch.object(m.subprocess,'Popen',return_value=process) as popen:
            self.assertEqual(m._run_checkpoint(['synthetic']),0)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(popen.call_args.kwargs['stdout'],subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs['stderr'],subprocess.PIPE)
        process.communicate.side_effect=[subprocess.TimeoutExpired('synthetic',180),subprocess.TimeoutExpired('synthetic',5),(b'',b'')]
        with mock.patch.object(m.subprocess,'Popen',return_value=process),mock.patch.object(m.os,'killpg') as kill:
            with self.assertRaises(m.MirrorHeld) as held:m._run_checkpoint(['synthetic'])
            self.assertEqual(held.exception.code,'checkpoint_timeout')
            self.assertEqual(kill.call_args_list,[mock.call(987654,m.signal.SIGTERM),mock.call(987654,m.signal.SIGKILL)])

    def test_workflow_only_prints_allowlisted_failure_code(self):
        workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/sync-wiki-to-notion.yml').read_text()
        self.assertIn('--stdout-file /tmp/notion-mirror-result.json -- python',workflow)
        step=workflow.split('      - name: Report safe mirror failure code\n',1)[1]
        self.assertIn('if: ${{ failure() }}',step)
        script=textwrap.dedent(step.split("python - <<'PYDIAG'\n",1)[1].rsplit('          PYDIAG',1)[0])
        for raw,expected in [
            (json.dumps({'status':'needs_review','failure_code':'checkpoint_publication_failed','reason':'PRIVATE_SENTINEL https://private.example/'}),'checkpoint_publication_failed'),
            (json.dumps({'status':'needs_review','failure_code':'PRIVATE_SENTINEL'}),'held'),
            ('PRIVATE_SENTINEL malformed','held'),
            ('[]','held'),
            ('PRIVATE_SENTINEL'*200,'held'),
        ]:
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'output.json';path.write_text(raw)
                result=REAL_RUN([sys.executable,'-c',script.replace('/tmp/notion-mirror-result.json',str(path))],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout,'Notion mirror failure_code='+expected+'\n')
                self.assertEqual(result.stderr,'')


if __name__=='__main__':unittest.main()

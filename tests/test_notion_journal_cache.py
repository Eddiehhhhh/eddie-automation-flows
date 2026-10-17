"""Verified immutable-object cache; real local filtered Git, no live services."""
from pathlib import Path
from git_object_fixture import remove_objects
import sys
import tempfile
import unittest
import zlib
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import get_notion_journal_runtime as rt
from test_notion_journal_runtime import Fixture, state, o, j, git_fixture

class CacheTests(unittest.TestCase):
    def test_verified_commit_fetch_once_but_identity_and_remote_guards_stay_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Fixture(directory); store = f.store()
            store.compare_and_set(o.digest(o.initial_state()), state())
            loader = rt.ExactJournalLoader(f.root, f); calls=[]
            metadata = f.metadata
            def fresh_identity(): calls.append('identity'); return metadata()
            f.metadata = fresh_identity
            start = len(f.fetch_args); head = f.head()
            loader(head); loader(head)
            self.assertEqual(sum('fetch' in args for args in f.fetch_args[start:]), 1)
            self.assertEqual(calls, ['identity', 'identity'])
            self.assertEqual(len(loader.verified_commits), 1)
            refs = store.local.run('for-each-ref'); index = (f.root / '.git/index').read_bytes()
            guard_count=len(f.guards); commit_reads=[]; original_commit=f.commit
            def fresh_commit(oid): commit_reads.append(oid); return original_commit(oid)
            f.commit=fresh_commit;store.load_commit=loader
            self.assertEqual(store.read(), (state(),o.digest(state())))
            self.assertGreater(len(f.guards),guard_count);self.assertGreaterEqual(len(commit_reads),2)
            self.assertEqual(sum('fetch' in args for args in f.fetch_args[start:]),1)
            self.assertEqual(store.local.run('for-each-ref'),refs)
            self.assertEqual((f.root / '.git/index').read_bytes(),index)

    def test_missing_or_corrupt_cached_state_stops_without_fetch_or_fallback(self):
        for corruption in ('missing','corrupt'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                f=Fixture(directory);store=f.store();store.compare_and_set(o.digest(o.initial_state()),state())
                loader=rt.ExactJournalLoader(f.root,f);head=f.head();loader(head)
                blob=store.anchor['state_blob'];path,_=remove_objects(f.root,blob)[blob]
                if corruption=='corrupt':
                    data=b'{}';path.write_bytes(zlib.compress(b'blob '+str(len(data)).encode()+b'\0'+data))
                    self.assertEqual(git_fixture.run(f.root,'cat-file','blob',blob),data)
                start=len(f.fetch_args);reads=len(f.blob_reads)
                with self.assertRaises(rt.CheckpointFailure):loader(head)
                self.assertEqual(len(f.fetch_args),start)
                self.assertFalse(any('fetch' in args for args in f.fetch_args[start:]))
                self.assertEqual(len(f.blob_reads),reads)
                self.assertEqual(len(f.calls),1)

if __name__=='__main__':unittest.main()

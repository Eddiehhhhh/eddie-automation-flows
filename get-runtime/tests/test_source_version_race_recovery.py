"""Narrow source-version-race recovery for an already mirrored Get note."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

TESTS=Path(__file__).resolve().parent
sys.path.insert(0,str(TESTS))
import test_hourly_duplicate_isolation as fixture

C=fixture.c
ENTRY=fixture.runtime.entry


class SourceVersionRaceRecovery(unittest.TestCase):
    def setUp(self):
        self.repo=fixture.HourlyDuplicateIsolation();self.repo.setUp()
        self.addCleanup(self.repo.doCleanups)
        self.ident='55555'
        self.summary=fixture.note(self.ident,'kept source text')
        path=C.new_path(self.summary,self.ident)
        data=C.render_exact(self.summary,path)
        self.repo.current[path]=data
        self.repo.notes[self.ident]=self.summary
        self.repo.owner_rows[self.ident]=C.record(self.summary,self.summary,path,data)
        self.path=path
        self.parent,self.legacy_sha,self.registry_sha=self.repo.save_parent()
        self.reader=C.legacy.OfflineGit(self.repo.root)

    def _make_race(self):
        changed_summary=deepcopy(self.summary);changed_summary['updated_at']+=1
        class Source:
            def enumerate(inner,source_id=None):
                terminal=True if source_id is None else {'contract':'get-single-note-source-scope-1','note_id':source_id,'complete_catalog':False}
                return [changed_summary],terminal
            def detail_raw(inner,ident):return C.legacy.encoded({'data':self.summary})
        current=C.git_inventory(self.reader,self.parent,only_id=self.ident)
        plan,_=ENTRY.engine.prepare(self.reader,self.repo.evidence,self.parent,
            current,Source(),legacy_sha=self.legacy_sha,registry_sha=self.registry_sha,
            source_id=self.ident)
        return plan

    def _retry(self, parent, detail):
        current=C.git_inventory(self.reader,parent,only_id=self.ident)
        class Source:
            calls=[]
            def enumerate(inner,source_id=None):
                terminal=True if source_id is None else {'contract':'get-single-note-source-scope-1','note_id':source_id,'complete_catalog':False}
                return [self.summary],terminal
            def detail_raw(inner,ident):
                inner.calls.append(ident)
                return C.legacy.encoded({'data':detail})
        source=Source()
        plan,files=ENTRY.engine.prepare(self.reader,self.repo.evidence,parent,
            current,source,legacy_sha=self.legacy_sha,
            registry_sha=C.legacy.sha(current[C.STATE]),
            source_id=self.ident)
        states=ENTRY.load_states(self.reader,self.repo.evidence,plan['candidate_commit'],
            plan['state_sha256'],plan['registry_sha256'],source_id=self.ident)
        return plan,states,plan['counts'],source.calls

    def test_stable_exact_id_reread_clears_only_the_race(self):
        raced=self._make_race()
        held=ENTRY.load_states(self.reader,self.repo.evidence,raced['candidate_commit'],
            raced['state_sha256'],raced['registry_sha256'],source_id=self.ident)
        self.assertEqual(held[2]['conflictsById'].get(self.ident),'source_version_race')
        plan,states,counts,calls=self._retry(raced['candidate_commit'],self.summary)
        self.assertEqual(calls,[self.ident])
        self.assertNotIn(self.ident,states[2]['conflictsById'])
        self.assertEqual(counts['new_state_only'],1)
        self.assertNotIn(self.path,{row['path'] for row in plan['changes']})

    def test_continuing_race_keeps_conflict_and_mirror_unchanged(self):
        raced=self._make_race();again=deepcopy(self.summary);again['updated_at']+=2
        plan,states,_,calls=self._retry(raced['candidate_commit'],again)
        self.assertEqual(calls,[self.ident])
        self.assertEqual(states[2]['conflictsById'][self.ident],'source_version_race')
        self.assertNotIn(self.path,{row['path'] for row in plan['changes']})

    def test_local_edit_is_preserved_and_blocks_recovery(self):
        raced=self._make_race()
        parent=raced['candidate_commit']
        target='Raw/03 Get/'+self.path
        original=self.reader.read(parent,target,C.legacy.owned.MAX_NOTE)
        self.repo.git('read-tree',parent)
        edited=original+b'\nlocal edit\n'
        blob=self.repo.git('hash-object','-w','--stdin',data=edited).decode().strip()
        self.repo.git('update-index','--add','--cacheinfo','100644,'+blob+','+target)
        tree=self.repo.git('write-tree').decode().strip()
        edited_parent=self.repo.git('commit-tree',tree,'-p',parent,
            data=b'preserve user edit\n').decode().strip()
        plan,states,_=C.prepare(self.reader,edited_parent,self.repo.evidence,
            raced['state_sha256'],raced['registry_sha256'],[self.summary],
            {'contract':'get-single-note-source-scope-1','note_id':self.ident,'complete_catalog':False},
            {self.ident:self.summary},only_id=self.ident)
        self.assertEqual(states['registry']['conflictsById'][self.ident],'source_version_race')
        self.assertEqual(self.reader.read(plan['candidate_commit'],target,
            C.legacy.owned.MAX_NOTE),edited)

    def test_other_conflict_reason_stays_blocked(self):
        registry={'reserved':{},'newById':{self.ident:{}},
                  'conflictsById':{self.ident:'source_unknown_fields'}}
        with self.assertRaises(Exception):
            ENTRY.engine.select_new({self.ident:self.summary},registry,None,
                source_id=self.ident)


if __name__=='__main__':unittest.main()

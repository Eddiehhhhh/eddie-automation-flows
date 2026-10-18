"""Synthetic status-only tests. No credentials, private journal or network."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import unittest
from unittest.mock import patch
import get_notion_saved_upload_status as d

ID='1'*32
NOW=datetime(2026,10,18,1,0,tzinfo=timezone.utc)

class Tests(unittest.TestCase):
    def slot(self):
        return {'state':{'upload_id':ID,'phase':'uploaded_verified'},
            'plan':{'filename':'synthetic.png','binding':{'mime':'image/png','bytes':12}}}
    def response(self):
        return {'object':'file_upload','id':ID,'status':'expired','expiry_time':'2026-10-17T23:00:00Z',
            'filename':'synthetic.png','content_type':'image/png','content_length':12,'archived':True}
    def observe(self,value,status=200):
        calls=[]
        def request(spec):calls.append(spec);return status,value
        with patch.object(d,'saved_slot',return_value=self.slot()):
            result=d.inspect_saved_upload(b'synthetic',request=request,now=lambda:NOW)
        return result,calls
    def test_only_saved_id_get_and_no_writes_or_private_output(self):
        value=self.response();result,calls=self.observe(value)
        self.assertEqual(result['status'],'inspected');self.assertEqual(result['upload_status'],'expired')
        self.assertTrue(result['observed_expired']);self.assertTrue(result['deadline_elapsed'])
        self.assertEqual(len(calls),1);self.assertEqual(calls[0],d.upload.spec('GET','/file_uploads/'+ID))
        self.assertEqual((result['external_write_attempts'],result['journal_writes']), (0,0))
        self.assertFalse(result['replacement_authorized'])
        public=json.dumps(result)
        for private in (ID,'synthetic.png','23:00:00','file_uploads'):self.assertNotIn(private,public)
    def test_elapsed_uploaded_is_not_expired_and_null_is_distinct(self):
        value=self.response();value.update(status='uploaded',archived=False)
        result,_=self.observe(value);self.assertFalse(result['observed_expired']);self.assertTrue(result['deadline_elapsed'])
        value['expiry_time']=None;result,_=self.observe(value)
        self.assertTrue(result['expiry_null']);self.assertIsNone(result['deadline_elapsed']);self.assertFalse(result['observed_expired'])
        value.update(status='failed',expiry_time='2026-10-18T03:00:00Z');result,_=self.observe(value)
        self.assertEqual(result['upload_status'],'failed');self.assertFalse(result['observed_expired']);self.assertFalse(result['deadline_elapsed'])
    def test_identity_metadata_schema_changes_stop(self):
        for key,value in [('id','2'*32),('object','page'),('filename','other.png'),('content_type','audio/ogg'),
                          ('content_length',True),('content_length',13),('status','private status'),
                          ('archived','true'),('in_trash',1),('expiry_time','2026-10-18T01:00:00')]:
            with self.subTest(key=key):
                changed=self.response();changed[key]=value;result,calls=self.observe(changed)
                self.assertEqual(result['status'],'blocked');self.assertNotIn('upload_status',result);self.assertEqual(len(calls),1)
        changed=self.response();del changed['expiry_time'];self.assertEqual(self.observe(changed)[0]['status'],'blocked')
    def test_permission_quota_and_read_failure_not_retried(self):
        for code,reason in [(401,'permission_denied'),(403,'permission_denied'),(429,'request_budget'),(500,'read_failed')]:
            result,calls=self.observe(self.response(),code);self.assertEqual(result['reason'],reason);self.assertEqual(len(calls),1)
    def test_exception_private_values_are_not_reported_or_retried(self):
        for reason,expected in [('permission_denied','permission_denied'),('PRIVATE https://secret.invalid token','inspection_failed')]:
            with patch.object(d,'saved_slot',return_value=self.slot()):
                calls=[]
                def request(spec):calls.append(spec);raise ValueError(reason)
                result=d.inspect_saved_upload(b'x',request=request,now=lambda:NOW)
            self.assertEqual(result['reason'],expected);self.assertEqual(len(calls),1);self.assertNotIn('PRIVATE',json.dumps(result))
    def test_bad_state_bytes_zero_requests(self):
        def forbidden(spec):self.fail('request before exact state validation')
        for value in (b'{}',{},None,b'PRIVATE'):
            result=d.inspect_saved_upload(value,request=forbidden,now=lambda:NOW)
            self.assertEqual(result['requests'],0);self.assertEqual(result['reason'],'state_changed')
    def test_state_plan_slot_and_id_pins_checked_before_request(self):
        slot=self.slot();note='1234567890123456789'
        plan={'records':[{'action':'create'},{'note_id':note,'action':'create'}]};op={'kind':'create'}
        state={'engine_state':{'journal':{'plan':plan,'operation':op,'index':0}},'png_assets':{'uploads':{note:slot}}}
        raw=d.outbox.encoded(state)
        pins={'STATE_BYTES':len(raw),'STATE_SHA':d.outbox.digest(raw),'PLAN_SHA':d.outbox.digest(plan),
            'OP_SHA':d.outbox.digest(op),'SLOT_SHA':d.outbox.digest(slot),'ID_SHA':d.outbox.digest(ID)}
        with patch.multiple(d,**pins),patch.object(d.outbox,'check_state'):
            self.assertEqual(d.saved_slot(raw),slot)
            for key in ('PLAN_SHA','OP_SHA','SLOT_SHA','ID_SHA'):
                with patch.object(d,key,'0'*64):
                    result=d.inspect_saved_upload(raw,request=lambda spec:self.fail('request'),now=lambda:NOW)
                    self.assertEqual(result['requests'],0);self.assertEqual(result['status'],'blocked')
    def test_default_import_has_no_execution_and_invalid_clock_zero_requests(self):
        with patch.object(d,'saved_slot',return_value=self.slot()):
            result=d.inspect_saved_upload(b'x',request=lambda spec:self.fail('request'),now=lambda:datetime(2026,1,1))
        self.assertEqual(result['reason'],'clock_invalid');self.assertEqual(result['requests'],0)

if __name__=='__main__':unittest.main()

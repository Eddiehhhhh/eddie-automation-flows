"""Read-only Get adapters for isolated mirror replay; no credential loading.

Uses existing reviewed HTTP transport and one shared RequestPolicy across both
passes/list/detail/retries. Existing 2MiB response/256MiB per-pass budgets and
150-page/2500-note caps are preserved. No workers and no schedule.
"""
import re
from get_v7_compat import api_id,note_identity

class MirrorSource:
 def __init__(self,key,client,policy,transport=None,budget_factory=None):
  if transport is None:
   import sync_getnote_to_raw as transport
  if budget_factory is None:
   from source_sync_inventory import ResponseBudget
   budget_factory=ResponseBudget
  self.key=key;self.client=client;self.policy=policy
  self.transport=transport;self.budget_factory=budget_factory
  self.budget=None;self.pages=0;self.detail_requests=0
 def enumerate(self):
  self.budget=self.budget_factory(256*1024*1024);self.pages=0;self.detail_requests=0
  items=[];ended=False
  for page,batch,more in self.transport.fetch_note_pages(self.key,self.client,
      max_pages=150,max_response_bytes=2*1024*1024,response_budget=self.budget,
      request_policy=self.policy):
   self.pages=page
   for item in batch:
    if not re.fullmatch(r'[0-9]{1,64}',note_identity(item)):raise ValueError('invalid_identity')
    items.append(item)
    if len(items)>2500:raise ValueError('note_limit')
   ended=more is False
  if not items or not ended:raise ValueError('incomplete_list')
  return items,True
 def detail(self,ident):
  if self.budget is None:raise ValueError('list_required')
  if not re.fullmatch(r'[0-9]{1,64}',ident):raise ValueError('invalid_identity')
  self.detail_requests+=1
  if self.detail_requests>2500:raise ValueError('detail_limit')
  data=self.transport.api_get(self.transport.DETAIL_PATH,
      {'id':ident,'image_quality':'original'},self.key,self.client,
      max_response_bytes=2*1024*1024,response_budget=self.budget,
      request_policy=self.policy)
  if not isinstance(data,dict):raise ValueError('invalid_detail')
  payload=data.get('data',data)
  if not isinstance(payload,dict):raise ValueError('invalid_detail')
  note=payload.get('note',payload)
  if not isinstance(note,dict):raise ValueError('invalid_detail')
  note_identity(note)
  return note

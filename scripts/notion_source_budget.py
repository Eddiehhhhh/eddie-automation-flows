"""Shared read-only source budgets; no source clients, state or credentials."""
import os
import time


def require(value,reason):
    if not value:raise ValueError(reason)


class BudgetExceeded(Exception):pass


class Budget:
    def __init__(self,limits,clock=time.monotonic,*,max_asset_bytes=50*1024*1024,max_batch_bytes=200*1024*1024):
        self.max_asset_bytes=max_asset_bytes;self.max_batch_bytes=max_batch_bytes
        self.clock=clock;self.started=clock();self.limits=limits;self.requests=0;self.pages=0;self.asset_bytes=0
        value=os.environ.get('NOTION_SOURCE_JOB_DEADLINE')
        require(value is None or value.isdigit(),'invalid_source_job_deadline')
        self.job_deadline=int(value) if value else None
    def check(self):
        if self.job_deadline is not None and time.time()>=self.job_deadline-300:
            raise BudgetExceeded('job_budget_reserved_for_persistence')
        if self.clock()-self.started>=self.limits['max_seconds']-min(60,self.limits['max_seconds']/4):
            raise BudgetExceeded('time_budget')
    def request(self):
        self.check()
        if self.requests>=self.limits['max_requests']:raise BudgetExceeded('request_budget')
        self.requests+=1
    def page(self):
        self.check()
        if self.pages>=self.limits['max_pages']:raise BudgetExceeded('page_budget')
        self.pages+=1
    def asset(self,raw):
        require(bool(raw),'empty_source_asset')
        require(len(raw)<=self.max_asset_bytes,'asset_file_budget')
        if self.asset_bytes+len(raw)>self.max_batch_bytes:raise BudgetExceeded('asset_batch_budget')
        self.asset_bytes+=len(raw)

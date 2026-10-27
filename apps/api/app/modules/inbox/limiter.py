"""Server-owned admission attempts; conservative configured Mac budget.

Source-idempotent retries are resolved before this bucket. A valid attempt spends
one token even if a later duplicate/transaction rejects it, preventing rollback
or duplicate storms from minting capacity. Restart gets a bounded fresh burst;
this process budget never decides score/ordering/ledger state.

Default budget is 5 attempts/second with burst 5. This is a throttle, not a
throughput guarantee. Current Mac evidence does not support promising either
10/second without rejection or the original 100-events/second burst target.
"""
import math
import threading
import time

class GlobalAdmissionLimiter:
    def __init__(self,rate=5,burst=5,clock=time.monotonic):
        if type(rate) is not int or type(burst) is not int or not 1<=rate<=10000 or not 1<=burst<=10000: raise ValueError('ADMISSION_BUDGET_INVALID')
        self.rate=rate; self.burst=burst; self.clock=clock
        self.tokens=float(burst); self.last=clock(); self.lock=threading.Lock()
    def allow(self):
        with self.lock:
            moment=self.clock()
            if not math.isfinite(moment) or moment<self.last: return False
            self.tokens=min(self.burst,self.tokens+(moment-self.last)*self.rate); self.last=moment
            if self.tokens<1: return False
            self.tokens-=1; return True

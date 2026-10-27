"""Real HTTP scoring with per-call clients and a server-owned cached monitor."""
import asyncio
from contextlib import asynccontextmanager
import time
import hashlib,json,math,threading
from collections import OrderedDict
from concurrent.futures import Future

# A successful probe stays authoritative for this long. It is deliberately longer
# than one probe interval and longer than the scoring deadline: a single slow
# encode must not flip a healthy model to not-ready.
OBSERVATION_FRESHNESS_SECONDS = 5.

class ScorerRuntime:
    def __init__(self,secret,manifest_id,manifest_sha256,dimension,*,client_factory=None,clock=time.monotonic,manifest_metadata=None):
        if client_factory is None:
            from .client import ScorerClient
            client_factory=ScorerClient
        self._metadata=None
        if manifest_metadata is not None:
            import rfc8785,copy
            if hashlib.sha256(rfc8785.dumps(manifest_metadata)).hexdigest()!=manifest_sha256: raise ValueError("MANIFEST_METADATA_MISMATCH")
            self._metadata=copy.deepcopy(manifest_metadata)
        self._factory=client_factory
        self._args=(secret,manifest_id,manifest_sha256,dimension)
        self._manifest_id=manifest_id
        self._clock=clock
        self._observation=None
        self._failures=0
        self._monitor=None
        self._cache=OrderedDict(); self._inflight={}; self._cache_lock=threading.Lock()
        self._cache_entries=4096; self._cache_bytes=1024*1024

    @property
    def manifest_metadata(self):
        import copy
        return copy.deepcopy(self._metadata)

    @property
    def manifest_id(self): return self._args[1]
    @property
    def manifest_sha256(self): return self._args[2]
    @property
    def dimension(self): return self._args[3]

    async def _call(self,method,*args):
        client=self._factory(*self._args,base_url='http://127.0.0.1:8124')
        try:return await getattr(client,method)(*args)
        finally:await client.aclose()

    async def score(self,word,frozen_puzzle):
        # Includes all frozen puzzle/calibration/target/normalization identity.
        raw=json.dumps({'manifest_sha256':self.manifest_sha256,'word':word,'puzzle':frozen_puzzle},sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        key=hashlib.sha256(raw).hexdigest()
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key); return self._cache[key]
            future=self._inflight.get(key); leader=future is None
            if leader:
                if len(self._inflight)>=200: raise ValueError('PAIR_CACHE_CAPACITY')
                future=Future(); self._inflight[key]=future
        if not leader: return await asyncio.shield(asyncio.wrap_future(future))
        try:
            value=await self._call('score',word,frozen_puzzle)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=99.9: raise ValueError('SCORER_RESULT_INVALID')
            with self._cache_lock:
                self._cache[key]=value
                while len(self._cache)>self._cache_entries or len(self._cache)*256>self._cache_bytes: self._cache.popitem(last=False)
            future.set_result(value); return value
        except BaseException as error:
            future.set_exception(error); raise
        finally:
            with self._cache_lock: self._inflight.pop(key,None)
    async def embeddings(self,texts):return await self._call('embeddings',texts)

    def readiness(self):
        # A single slow/failed probe is not a dead model: one encode can miss the
        # client deadline while later calls succeed. Two consecutive failures (about
        # two probe intervals) still fail closed before any settlement, and the
        # freshness window keeps a silent stall from counting as healthy.
        observation=self._observation
        ready=observation is not None and 0<=self._clock()-observation<OBSERVATION_FRESHNESS_SECONDS and self._failures<2
        return {'ready':ready,'manifest_id':self._manifest_id if ready else None}

    async def probe(self):
        # Client verifies HMAC, manifest, dimension, finite normalized vectors.
        try:
            await self.embeddings(['词温'])
            self._observation=self._clock(); self._failures=0
        except asyncio.CancelledError:raise
        except Exception:
            self._failures+=1
            if self._failures>=2:self._observation=None

    async def _run(self):
        try:
            while True:
                started=self._clock()
                await self.probe()
                await asyncio.sleep(max(0.,1.-(self._clock()-started)))
        finally:self._observation=None

    async def start(self):
        if self._monitor is None:self._monitor=asyncio.create_task(self._run(),name='scorer-model-monitor')

    async def close(self):
        if self._monitor is not None:
            task=self._monitor;self._monitor=None
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        self._observation=None

    def wrap_lifespan(self,app):
        original=app.router.lifespan_context
        @asynccontextmanager
        async def lifespan(application):
            async with original(application):
                await self.start()
                try:yield
                finally:await self.close()
        app.router.lifespan_context=lifespan
        return app

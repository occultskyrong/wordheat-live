"""One inference worker, finite queue/text batches, monotonic request budgets."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import hashlib
from dataclasses import dataclass
import time

from .protocol import validate_vectors


class BatchFailure(ValueError):
    pass


@dataclass
class Pending:
    texts: list
    expires: float
    future: asyncio.Future


class Batcher:
    def __init__(self, encoder, dimension, normalize, queue_capacity=200, window_ms=20, cache_entries=1024, cache_bytes=8 * 1024 * 1024):
        if not 1 <= queue_capacity <= 200 or not 0 <= window_ms <= 50: raise ValueError('BATCH_LIMIT_INVALID')
        if not 0 <= cache_entries <= 4096 or not 0 <= cache_bytes <= 32 * 1024 * 1024: raise ValueError('CACHE_LIMIT_INVALID')
        self.cache = OrderedDict()
        self.cache_entries, self.cache_bytes, self.cache_size = cache_entries, cache_bytes, 0
        self.encoder, self.dimension, self.normalize = encoder, dimension, normalize
        self.queue = asyncio.Queue(maxsize=queue_capacity)
        self.window_ms = window_ms
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wordheat-inference')
        self.worker = None
        self.closed = False
        self.pending = set()

    async def start(self):
        self.worker = asyncio.create_task(self._run())

    async def submit(self, texts, deadline_ms):
        if self.closed: raise BatchFailure('SCORER_CLOSED')
        future = asyncio.get_running_loop().create_future()
        item = Pending(texts, time.monotonic() + deadline_ms / 1000, future)
        try: self.queue.put_nowait(item)
        except asyncio.QueueFull as exc: raise BatchFailure('QUEUE_FULL') from exc
        self.pending.add(future)
        future.add_done_callback(self.pending.discard)
        try:
            return await asyncio.wait_for(future, deadline_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise BatchFailure('DEADLINE_EXCEEDED') from exc

    async def _run(self):
        carry = None
        while True:
            first = carry or await self.queue.get()
            carry = None
            batch = [first]
            count = len(first.texts)
            await asyncio.sleep(min(self.window_ms / 1000, max(0, first.expires - time.monotonic())))
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if count + len(item.texts) > 32:
                    carry = item
                    break
                batch.append(item)
                count += len(item.texts)
            batch = [item for item in batch if not item.future.done() and item.expires > time.monotonic()]
            if not batch: continue
            texts = [text for item in batch for text in item.texts]
            try:
                keys = [hashlib.sha256(text.encode('utf-8')).digest() for text in texts]
                resolved = {}
                missing = {}
                for key, text in zip(keys, texts):
                    if key in self.cache:
                        self.cache.move_to_end(key)
                        resolved[key] = self.cache[key]
                    else: missing.setdefault(key, text)
                if missing:
                    encoded = await asyncio.get_running_loop().run_in_executor(self.executor, self.encoder.encode, list(missing.values()))
                    encoded = validate_vectors(encoded, len(missing), self.dimension, self.normalize)
                    for key, vector in zip(missing, encoded):
                        resolved[key] = vector
                        entry_bytes = self.dimension * 32 + 256
                        if self.cache_entries and entry_bytes <= self.cache_bytes:
                            self.cache[key] = vector
                            self.cache_size += entry_bytes
                            while len(self.cache) > self.cache_entries or self.cache_size > self.cache_bytes:
                                self.cache.popitem(last=False)
                                self.cache_size -= entry_bytes
                vectors = [resolved[key] for key in keys]
                offset = 0
                for item in batch:
                    size = len(item.texts)
                    if not item.future.done():
                        if item.expires <= time.monotonic(): item.future.set_exception(BatchFailure('DEADLINE_EXCEEDED'))
                        else: item.future.set_result(vectors[offset:offset + size])
                    offset += size
            except Exception:
                for item in batch:
                    if not item.future.done(): item.future.set_exception(BatchFailure('MODEL_FAILED'))

    async def close(self):
        self.closed = True
        for future in tuple(self.pending):
            if not future.done(): future.set_exception(BatchFailure('SCORER_CLOSED'))
        if self.worker:
            self.worker.cancel()
            try: await self.worker
            except asyncio.CancelledError: pass
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if not item.future.done(): item.future.set_exception(BatchFailure('SCORER_CLOSED'))
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)

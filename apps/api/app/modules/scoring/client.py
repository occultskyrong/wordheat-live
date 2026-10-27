"""Private HTTP scorer client. No imports from the independent scorer app."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import re
import time
import uuid
from collections import OrderedDict

import httpx
import rfc8785

PATH = '/internal/v1/embeddings'


class ScorerFailure(Exception):
    def __init__(self, code, message=None, status_code=503, retryable=True, details=None):
        self.code = code
        self.message = message or code
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        super().__init__(self.message)


def _canonical(value):
    return rfc8785.dumps(value)


def _sign(secret, direction, request_id, body, manifest_sha256, issued_at):
    message = _canonical({'protocol': 'wordheat-embeddings/1', 'direction': direction,
                          'method': 'POST', 'path': PATH, 'request_id': request_id,
                          'body_sha256': hashlib.sha256(body).hexdigest(),
                          'manifest_sha256': manifest_sha256, 'issued_at': issued_at})
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _vectors(vectors, count, dimension):
    if not isinstance(vectors, list) or len(vectors) != count: raise ValueError('vector count')
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension: raise ValueError('vector dimension')
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in vector): raise ValueError('nonfinite vector')
        norm = math.sqrt(sum(v * v for v in vector))
        if not math.isfinite(norm) or abs(norm - 1.) > .001: raise ValueError('vector norm')
    return vectors


class ScorerClient:
    def __init__(self, secret: bytes, manifest_id: str, manifest_sha256: str, dimension: int,
                 base_url='http://127.0.0.1:8124', *, http=None, deadline_ms=2000):
        if not isinstance(secret, bytes) or len(secret) < 32: raise ValueError('BOOT_SECRET_REQUIRED')
        if not re.fullmatch('[0-9a-f]{64}', manifest_sha256): raise ValueError('MANIFEST_SHA_REQUIRED')
        if not isinstance(manifest_id, str) or not manifest_id or type(dimension) is not int or not 1 <= dimension <= 4096:
            raise ValueError('MODEL_IDENTITY_REQUIRED')
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 10000: raise ValueError('DEADLINE_INVALID')
        url = httpx.URL(str(http.base_url) if http is not None else base_url)
        try: loopback = ipaddress.ip_address(url.host).is_loopback
        except ValueError: loopback = False
        if url.scheme != 'http' or not loopback or url.userinfo or url.query or url.path not in ('', '/'):
            raise ValueError('LOOPBACK_SCORER_REQUIRED')
        self._secret = secret
        self.manifest_id, self.manifest_sha256, self.dimension = manifest_id, manifest_sha256, dimension
        self.deadline_ms = deadline_ms
        self._scores=OrderedDict()
        self._owned_http = http is None
        self.http = http or httpx.AsyncClient(base_url=base_url, timeout=deadline_ms / 1000, trust_env=False, follow_redirects=False)

    async def aclose(self):
        if self._owned_http: await self.http.aclose()

    async def embeddings(self, texts):
        if not isinstance(texts, list) or not 1 <= len(texts) <= 32 or any(not isinstance(t, str) or not 1 <= len(t) <= 256 for t in texts):
            raise ScorerFailure('SCORER_INPUT_INVALID', status_code=422, retryable=False)
        mono_deadline = time.monotonic() + self.deadline_ms / 1000
        request_id = uuid.uuid4().hex
        issued_at = str(time.time_ns() // 1000000)
        raw = _canonical({'request_id': request_id, 'manifest_id': self.manifest_id, 'texts': texts, 'deadline_ms': self.deadline_ms})
        headers = {'content-type': 'application/json', 'x-wordheat-issued-at': issued_at,
                   'x-wordheat-signature': _sign(self._secret, 'request', request_id, raw, self.manifest_sha256, issued_at)}
        try:
            async with asyncio.timeout(max(0., mono_deadline - time.monotonic())):
                # Streaming bounds response allocation even when the peer malfunctions.
                async with self.http.stream('POST', PATH, content=raw, headers=headers,
                                            timeout=self.deadline_ms / 1000, follow_redirects=False) as response:
                    if response.status_code != 200: raise ScorerFailure('SCORER_REJECTED')
                    result = bytearray()
                    async for chunk in response.aiter_bytes():
                        result.extend(chunk)
                        if len(result) > 4 * 1024 * 1024: raise ScorerFailure('SCORER_RESPONSE_TOO_LARGE')
                    result = bytes(result)
                    signature = response.headers.get('x-wordheat-signature', '')
                    expected = _sign(self._secret, 'response', request_id, result, self.manifest_sha256, issued_at)
                    if not re.fullmatch('[0-9a-f]{64}', signature) or not hmac.compare_digest(signature, expected):
                        raise ScorerFailure('SCORER_AUTH_FAILED')
                    data = _parse(result)
                    if result != _canonical(data): raise ValueError('noncanonical')
                    if set(data) != {'request_id', 'manifest_id', 'dimension', 'vectors'} or data['request_id'] != request_id or data['manifest_id'] != self.manifest_id or type(data['dimension']) is not int or data['dimension'] != self.dimension:
                        raise ScorerFailure('SCORER_MODEL_MISMATCH')
                    vectors = _vectors(data['vectors'], len(texts), self.dimension)
                # Includes stream close and synchronous signature/JSON/vector validation.
                if time.monotonic() >= mono_deadline:
                    raise ScorerFailure('SCORER_TIMEOUT', status_code=504)
                return vectors
        except ScorerFailure:
            raise
        except (httpx.TimeoutException, TimeoutError) as exc: raise ScorerFailure('SCORER_TIMEOUT', status_code=504) from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ScorerFailure('SCORER_RESPONSE_INVALID') from exc

    async def score(self, word: str, frozen_puzzle: dict) -> float:
        try:
            if frozen_puzzle['manifest_id'] != self.manifest_id: raise ValueError('manifest')
            if frozen_puzzle.get('manifest_sha256',self.manifest_sha256)!=self.manifest_sha256 or frozen_puzzle.get('dimension',self.dimension)!=self.dimension or frozen_puzzle.get('normalization','unit-l2/1')!='unit-l2/1': raise ValueError('frozen identity')
            cache_key=hashlib.sha256(_canonical({'manifest_sha256':self.manifest_sha256,'word':word,'puzzle':frozen_puzzle})).hexdigest()
            if cache_key in self._scores:
                self._scores.move_to_end(cache_key); return self._scores[cache_key]
            calibration = frozen_puzzle['calibration']
            b, u, gamma = (calibration[key] for key in ('b', 'u', 'gamma'))
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (b, u, gamma)) or not u > b or not gamma > 0:
                raise ValueError('calibration')
            target = frozen_puzzle.get('target_vector')
            if target is None:
                answer = frozen_puzzle.get('word', frozen_puzzle.get('answer'))
                if not isinstance(answer, str) or not answer: raise ValueError('answer')
                guess_vector, target = await self.embeddings([word, answer])
            else:
                _vectors([target], 1, self.dimension)
                guess_vector = (await self.embeddings([word]))[0]
            raw = sum(x * y for x, y in zip(guess_vector, target))
            if not math.isfinite(raw): raise ValueError('nonfinite score')
            scaled = 99.9 * min(1., max(0., (raw - b) / (u - b))) ** gamma
            value=min(99.9,round(scaled,1)); self._scores[cache_key]=value
            while len(self._scores)>4096: self._scores.popitem(last=False)
            return value
        except ScorerFailure:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ScorerFailure('SCORER_FROZEN_PUZZLE_INVALID', status_code=422, retryable=False) from exc

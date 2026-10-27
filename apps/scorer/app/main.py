"""Factory called by a trusted launcher with an in-memory per-boot secret."""
from contextlib import asynccontextmanager
from pathlib import Path
import time
from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .batcher import Batcher, BatchFailure
from .encoder import Encoder
from .manifest import load_manifest
from .protocol import SIGNATURE_HEADER, canonical, parse_json, sign, verify, validate_vectors

Text = Annotated[StrictStr, Field(min_length=1, max_length=256)]
Identity = Annotated[StrictStr, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    request_id: Identity
    manifest_id: Identity
    texts: Annotated[list[Text], Field(min_length=1, max_length=32)]
    deadline_ms: Annotated[StrictInt, Field(ge=1, le=10000)]


def create_app(secret: bytes, manifest_path: Path, model_root: Path, encoder=None):
    if not isinstance(secret, bytes) or len(secret) < 32: raise ValueError('BOOT_SECRET_REQUIRED')
    manifest = load_manifest(Path(manifest_path), Path(model_root))
    replay = {}
    # 30s future allowance + 30s age + 500ms clock drift + safety margin.
    replay_ttl = 62.
    replay_capacity = 10000
    boot_wall, boot_mono = time.time(), time.monotonic()
    last_mono = boot_mono
    clock_failed = False

    def trusted_clock():
        nonlocal last_mono, clock_failed
        wall, mono = time.time(), time.monotonic()
        if (clock_failed or mono < last_mono
                or abs((wall - boot_wall) - (mono - boot_mono)) > .5):
            clock_failed = True
            app.state.ready = False
            return None
        last_mono = mono
        return wall, mono

    @asynccontextmanager
    async def lifespan(app):
        app.state.ready = False
        active = encoder if encoder is not None else Encoder(manifest)
        # Injected test encoders undergo the same warmup/vector validation.
        validate_vectors(active.encode(manifest.data.get('warmup_texts', ['预热'])),
                         len(manifest.data.get('warmup_texts', ['预热'])), manifest.dimension, manifest.normalize)
        batcher = Batcher(active, manifest.dimension, manifest.normalize)
        app.state.batcher = batcher
        await batcher.start()
        app.state.ready = True
        try: yield
        finally:
            app.state.ready = False
            await batcher.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manifest = manifest
    app.state.ready = False

    @app.post('/internal/v1/embeddings')
    async def embeddings(request: Request):
        def fail(code, status):
            return JSONResponse({'error': {'code': code, 'message': code, 'retryable': status >= 500}}, status_code=status)
        # Binding must also be loopback at launcher; do not accept proxy headers.
        host = request.client.host if request.client else ''
        if host not in ('127.0.0.1', '::1'): return fail('LOOPBACK_REQUIRED', 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536: return fail('BODY_TOO_LARGE', 413)
        try:
            value = parse_json(bytes(raw))
            body = EmbeddingRequest.model_validate(value)
            if bytes(raw) != canonical(value): return fail('CANONICAL_JSON_REQUIRED', 400)
        except (ValueError, TypeError, ValidationError): return fail('INVALID_REQUEST', 422)
        clock = trusted_clock()
        if clock is None: return fail('BOOT_CLOCK_UNTRUSTED', 503)
        wall, now = clock
        issued_at = request.headers.get('x-wordheat-issued-at', '')
        if not issued_at.isascii() or not issued_at.isdecimal() or len(issued_at) != 13 or abs(wall * 1000 - int(issued_at)) > 30000:
            return fail('REQUEST_EXPIRED', 401)
        if not verify(secret, 'request', body.request_id, bytes(raw), manifest.sha256, request.headers.get(SIGNATURE_HEADER), issued_at):
            return fail('SCORER_AUTH_FAILED', 401)
        if body.manifest_id != manifest.manifest_id: return fail('MANIFEST_MISMATCH', 409)
        for key in [key for key, expiry in replay.items() if expiry <= now]: del replay[key]
        if body.request_id in replay: return fail('REQUEST_REPLAY', 409)
        if len(replay) >= replay_capacity: return fail('REPLAY_CAPACITY', 503)
        replay[body.request_id] = now + replay_ttl
        if not app.state.ready: return fail('MODEL_NOT_READY', 503)
        try: vectors = await app.state.batcher.submit(body.texts, body.deadline_ms)
        except BatchFailure as exc:
            code = str(exc)
            return fail(code, 504 if code == 'DEADLINE_EXCEEDED' else 503)
        if trusted_clock() is None: return fail('BOOT_CLOCK_UNTRUSTED', 503)
        response = canonical({'request_id': body.request_id, 'manifest_id': manifest.manifest_id,
                              'dimension': manifest.dimension, 'vectors': vectors})
        return Response(response, media_type='application/json', headers={SIGNATURE_HEADER: sign(secret, 'response', body.request_id, response, manifest.sha256, issued_at)})
    return app

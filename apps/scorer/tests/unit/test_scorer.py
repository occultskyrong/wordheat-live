import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def api_client_module():
    path = Path(__file__).resolve().parents[3] / 'api/app/modules/scoring/client.py'
    spec = importlib.util.spec_from_file_location('scoring_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def model(tmp_path):
    root = tmp_path / 'model'
    root.mkdir()
    f = root / 'config.json'
    f.write_text('{}')
    data = dict(manifest_id='fixture-v1', model_id='fixture-only', revision='a' * 40,
                license='MIT', dimension=2, normalize=True, pooling='cls', precision='float32',
                tokenizer={'revision': 'a' * 40}, preprocessing='identity',
                warmup_texts=['测试'], calibration_version='fixture', wordbank_version='fixture',
                files=[dict(path='config.json', size=2, sha256=hashlib.sha256(b'{}').hexdigest())])
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(data))
    return root, path, data


class FixtureEncoder:
    """Protocol component fixture; never model quality evidence."""
    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


def test_feature_entrypoint_exists():
    assert (Path(__file__).resolve().parents[2] / 'app/main.py').exists(), 'scorer not implemented'


def test_manifest_rejects_missing_hash_path_escape_and_symlink(model):
    from app.manifest import load_manifest, ManifestFailure
    root, path, data = model
    assert load_manifest(path, root).dimension == 2
    for mutation in ('missing', 'hash', 'escape', 'symlink'):
        item = dict(data['files'][0])
        if mutation == 'missing': item['path'] = 'missing'
        if mutation == 'hash': item['sha256'] = '0' * 64
        if mutation == 'escape': item['path'] = '../config.json'
        if mutation == 'symlink':
            (root / 'link.json').symlink_to(root / 'config.json')
            item['path'] = 'link.json'
        path.write_text(json.dumps({**data, 'files': [item]}))
        with pytest.raises(ManifestFailure): load_manifest(path, root)


def signed(client, data, secret=b'x' * 32, digest=None):
    from app.protocol import canonical, sign
    raw = canonical(data)
    issued_at = str(time.time_ns() // 1000000)
    digest = digest or client.app.state.manifest.sha256
    return client.post('/internal/v1/embeddings', content=raw, headers={
        'content-type': 'application/json', 'x-wordheat-issued-at': issued_at, 'x-wordheat-signature': sign(secret, 'request', data['request_id'], raw, digest, issued_at)})


def payload(**kwargs):
    return dict(request_id=uuid.uuid4().hex, manifest_id='fixture-v1', texts=['测试'], deadline_ms=1000, **kwargs)


def test_authenticated_response_replay_and_wrong_identity(model):
    from app.main import create_app
    from app.protocol import verify
    root, path, _ = model
    with TestClient(create_app(b'x' * 32, path, root, FixtureEncoder()), client=('127.0.0.1', 50000)) as client:
        data = payload()
        response = signed(client, data)
        assert response.status_code == 200
        assert response.json()['vectors'] == [[1., 0.]]
        assert verify(b'x' * 32, 'response', data['request_id'], response.content,
                      client.app.state.manifest.sha256, response.headers['x-wordheat-signature'], response.request.headers['x-wordheat-issued-at'])
        assert signed(client, data).status_code == 409
        assert signed(client, payload(), secret=b'y' * 32).status_code == 401
        wrong = payload(); wrong['manifest_id'] = 'other'
        assert signed(client, wrong).status_code == 409
        assert client.get('/docs').status_code == 404
        assert client.get('/openapi.json').status_code == 404


@pytest.mark.parametrize('field,value', [('deadline_ms', True), ('deadline_ms', 0), ('texts', []), ('texts', ['x'] * 33), ('texts', [1]), ('request_id', ''), ('manifest_id', 1)])
def test_strict_requests(model, field, value):
    from app.main import create_app
    root, path, _ = model
    with TestClient(create_app(b'x' * 32, path, root, FixtureEncoder()), client=('127.0.0.1', 50000)) as client:
        data = payload(); data[field] = value
        assert signed(client, data).status_code == 422


def test_nonfinite_and_wrong_dimension_rejected(model):
    from app.main import create_app
    root, path, _ = model
    class Broken:
        def __init__(self, vec): self.vec = vec
        def encode(self, texts): return [self.vec] * len(texts)
    for vec in ([float('nan'), 0.], [0., 0.], [1.], [2., 0.]):
        with pytest.raises(ValueError):
            with TestClient(create_app(b'x' * 32, path, root, Broken(vec))): pass


def test_batched_queue_timeout_and_capacity():
    from app.batcher import Batcher, BatchFailure
    class Counting(FixtureEncoder):
        calls = []
        def encode(self, texts):
            self.calls.append(texts)
            time.sleep(.03)
            return super().encode(texts)
    async def run():
        encoder = Counting()
        batcher = Batcher(encoder, dimension=2, normalize=True, queue_capacity=2, window_ms=20)
        await batcher.start()
        first = asyncio.create_task(batcher.submit(['a'], 1000))
        second = asyncio.create_task(batcher.submit(['b'], 1000))
        await asyncio.sleep(0)
        with pytest.raises(BatchFailure, match='QUEUE_FULL'): await batcher.submit(['c'], 1000)
        assert await first == [[1., 0.]]
        assert await second == [[1., 0.]]
        assert encoder.calls == [['a', 'b']]
        with pytest.raises(BatchFailure, match='DEADLINE_EXCEEDED'): await batcher.submit(['c'], 1)
        await batcher.close()
    asyncio.run(run())


def test_client_calibration_and_signature(model):
    from app.main import create_app
    root, path, _ = model
    module = api_client_module()
    app = create_app(b'x' * 32, path, root, FixtureEncoder())
    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8124') as http:
                scorer = module.ScorerClient(b'x' * 32, 'fixture-v1', app.state.manifest.sha256, 2, http=http)
                puzzle = {'manifest_id': 'fixture-v1', 'answer': '苹果', 'calibration': {'b': 0., 'u': 1., 'gamma': 1.}}
                assert await scorer.score('梨子', puzzle) == 99.9
                with pytest.raises(module.ScorerFailure): await scorer.score('梨子', {'manifest_id': 'fixture-v1', 'answer': '苹果'})
    asyncio.run(run())


def test_expired_request_and_nonloopback_are_rejected(model):
    from app.main import create_app
    from app.protocol import canonical, sign
    root, path, _ = model
    app = create_app(b'x' * 32, path, root, FixtureEncoder())
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        data = payload(); raw = canonical(data); issued_at = str(int(time.time() * 1000) - 31000)
        response = client.post('/internal/v1/embeddings', content=raw, headers={
            'x-wordheat-issued-at': issued_at,
            'x-wordheat-signature': sign(b'x' * 32, 'request', data['request_id'], raw, app.state.manifest.sha256, issued_at)})
        assert response.status_code == 401
    with TestClient(app, client=('192.0.2.1', 50000)) as client:
        assert signed(client, payload()).status_code == 403


@pytest.mark.parametrize('kind', ['signature', 'identity', 'dimension', 'nonfinite', 'norm'])
def test_client_rejects_untrusted_or_invalid_response(kind):
    module = api_client_module()
    async def run():
        async def respond(request):
            data = json.loads(request.content)
            body = dict(request_id=data['request_id'], manifest_id='fixture-v1', dimension=2, vectors=[[1., 0.]])
            if kind == 'identity': body['request_id'] = 'wrong-request'
            if kind == 'dimension': body['dimension'] = 3
            if kind == 'nonfinite': body['vectors'] = [[float('nan'), 0.]]
            if kind == 'norm': body['vectors'] = [[2., 0.]]
            raw = json.dumps(body, separators=(',', ':')).encode() if kind == 'nonfinite' else module._canonical(body)
            signature = module._sign(b'x' * 32, 'response', data['request_id'], raw, 'a' * 64, request.headers['x-wordheat-issued-at'])
            if kind == 'signature': signature = '0' * 64
            return httpx.Response(200, content=raw, headers={'x-wordheat-signature': signature})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://127.0.0.1:8124') as http:
            scorer = module.ScorerClient(b'x' * 32, 'fixture-v1', 'a' * 64, 2, http=http)
            with pytest.raises(module.ScorerFailure): await scorer.embeddings(['测试'])
    asyncio.run(run())


def test_warmup_failure_keeps_not_ready(model):
    from app.main import create_app
    root, path, _ = model
    class Failing:
        def encode(self, texts): raise RuntimeError('load failure')
    app = create_app(b'x' * 32, path, root, Failing())
    with pytest.raises(RuntimeError):
        with TestClient(app): pass
    assert app.state.ready is False


def test_duplicate_keys_and_manifest_digest_mismatch_rejected(model):
    from app.main import create_app
    root, path, _ = model
    with TestClient(create_app(b'x' * 32, path, root, FixtureEncoder()), client=('127.0.0.1', 50000)) as client:
        assert signed(client, payload(), digest='0' * 64).status_code == 401
        raw = b'{"request_id":"one","request_id":"two"}'
        assert client.post('/internal/v1/embeddings', content=raw).status_code == 422


def test_batch_shutdown_resolves_inflight_request():
    from app.batcher import Batcher, BatchFailure
    class Slow(FixtureEncoder):
        def encode(self, texts):
            time.sleep(.05)
            return super().encode(texts)
    async def run():
        batcher = Batcher(Slow(), 2, True, window_ms=0)
        await batcher.start()
        item = asyncio.create_task(batcher.submit(['a'], 1000))
        await asyncio.sleep(.01)
        await batcher.close()
        with pytest.raises(BatchFailure, match='SCORER_CLOSED'):
            await asyncio.wait_for(item, .1)
    asyncio.run(run())


def test_same_text_coalesces_and_cache_is_bounded():
    from app.batcher import Batcher
    class Counting(FixtureEncoder):
        calls = []
        def encode(self, texts):
            self.calls.append(texts)
            return super().encode(texts)
    async def run():
        encoder = Counting()
        batcher = Batcher(encoder, 2, True, cache_entries=1, cache_bytes=1024)
        await batcher.start()
        await asyncio.gather(batcher.submit(['a'], 1000), batcher.submit(['a'], 1000))
        assert encoder.calls == [['a']]
        await batcher.submit(['a'], 1000)
        assert encoder.calls == [['a']]
        await batcher.submit(['b'], 1000)
        await batcher.submit(['a'], 1000)
        assert encoder.calls == [['a'], ['b'], ['a']]
        assert batcher.cache_size <= 1024
        await batcher.close()
    asyncio.run(run())


def test_http_full_batch_and_deadline(model):
    from app.main import create_app
    root, path, _ = model
    with TestClient(create_app(b'x' * 32, path, root, FixtureEncoder()), client=('127.0.0.1', 50000)) as client:
        data = payload(); data['texts'] = [str(i) for i in range(32)]
        response = signed(client, data)
        assert response.status_code == 200
        assert len(response.json()['vectors']) == 32
        data = payload(); data['deadline_ms'] = 1
        assert signed(client, data).status_code == 504


def test_runtime_nonfinite_is_model_failure(model):
    from app.main import create_app
    root, path, _ = model
    class FailsAfterWarmup(FixtureEncoder):
        calls = 0
        def encode(self, texts):
            self.calls += 1
            return super().encode(texts) if self.calls == 1 else [[float('nan'), 0.]] * len(texts)
    with TestClient(create_app(b'x' * 32, path, root, FailsAfterWarmup()), client=('127.0.0.1', 50000)) as client:
        response = signed(client, payload())
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'MODEL_FAILED'


def test_boot_clock_rollback_never_revalidates_expired_request(model, monkeypatch):
    import types
    import app.main as main
    from app.protocol import canonical, sign
    root, path, _ = model
    wall = time.time()
    clock = {'wall': wall, 'mono': time.monotonic()}
    monkeypatch.setattr(main, 'time', types.SimpleNamespace(time=lambda: clock['wall'], monotonic=lambda: clock['mono']))
    app = main.create_app(b'x' * 32, path, root, FixtureEncoder())
    data = payload(); raw = canonical(data); issued_at = str(int(wall * 1000))
    headers = {'x-wordheat-issued-at': issued_at,
               'x-wordheat-signature': sign(b'x' * 32, 'request', data['request_id'], raw, app.state.manifest.sha256, issued_at)}
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        send = lambda: client.post('/internal/v1/embeddings', content=raw, headers=headers)
        assert send().status_code == 200
        assert send().status_code == 409
        clock['wall'] += 61; clock['mono'] += 61
        assert send().status_code == 401
        clock['wall'] -= 61
        response = send()
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'BOOT_CLOCK_UNTRUSTED'
        clock['wall'] += 61
        assert send().status_code == 503
        assert app.state.ready is False


def test_replay_retention_covers_future_timestamp_and_drift_margin(model, monkeypatch):
    import types
    import app.main as main
    from app.protocol import canonical, sign
    root, path, _ = model
    wall = time.time()
    clock = {'wall': wall, 'mono': time.monotonic()}
    monkeypatch.setattr(main, 'time', types.SimpleNamespace(time=lambda: clock['wall'], monotonic=lambda: clock['mono']))
    app = main.create_app(b'x' * 32, path, root, FixtureEncoder())
    data = payload(); raw = canonical(data); issued_at = str(int((wall + 29.99) * 1000))
    headers = {'x-wordheat-issued-at': issued_at,
               'x-wordheat-signature': sign(b'x' * 32, 'request', data['request_id'], raw, app.state.manifest.sha256, issued_at)}
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        send = lambda: client.post('/internal/v1/embeddings', content=raw, headers=headers)
        assert send().status_code == 200
        clock['mono'] += 60.1; clock['wall'] += 59.9
        assert send().status_code == 409


@pytest.mark.parametrize('drift', [-.501, .501])
def test_boot_clock_jump_beyond_500ms_fails_closed(model, monkeypatch, drift):
    import types
    import app.main as main
    root, path, _ = model
    clock = {'wall': time.time(), 'mono': time.monotonic()}
    monkeypatch.setattr(main, 'time', types.SimpleNamespace(time=lambda: clock['wall'], monotonic=lambda: clock['mono']))
    app = main.create_app(b'x' * 32, path, root, FixtureEncoder())
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        clock['wall'] += drift
        assert signed(client, payload()).status_code == 503


def test_client_total_deadline_rejects_valid_slow_drip_response():
    module = api_client_module()
    class SlowStream(httpx.AsyncByteStream):
        def __init__(self, raw): self.raw = raw
        async def __aiter__(self):
            for offset in range(0, len(self.raw), 20):
                await asyncio.sleep(.02)
                yield self.raw[offset:offset + 20]
    async def run():
        async def respond(request):
            body = json.loads(request.content)
            raw = module._canonical({'request_id': body['request_id'], 'manifest_id': 'fixture-v1',
                                     'dimension': 2, 'vectors': [[1., 0.]]})
            signature = module._sign(b'x' * 32, 'response', body['request_id'], raw, 'a' * 64, request.headers['x-wordheat-issued-at'])
            return httpx.Response(200, stream=SlowStream(raw), headers={'x-wordheat-signature': signature})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://127.0.0.1:8124') as http:
            scorer = module.ScorerClient(b'x' * 32, 'fixture-v1', 'a' * 64, 2, http=http, deadline_ms=40)
            started = time.monotonic()
            with pytest.raises(module.ScorerFailure) as error:
                await scorer.embeddings(['测试'])
            assert error.value.code == 'SCORER_TIMEOUT'
            assert error.value.status_code == 504
            assert time.monotonic() - started < .12
    asyncio.run(run())


def test_client_checks_monotonic_budget_after_synchronous_validation(monkeypatch):
    module = api_client_module()
    original_vectors = module._vectors
    def slow_validation(*args):
        time.sleep(.05)  # event loop cannot interrupt CPU validation; final clock check must reject
        return original_vectors(*args)
    monkeypatch.setattr(module, '_vectors', slow_validation)
    async def run():
        async def respond(request):
            body = json.loads(request.content)
            raw = module._canonical({'request_id': body['request_id'], 'manifest_id': 'fixture-v1',
                                     'dimension': 2, 'vectors': [[1., 0.]]})
            signature = module._sign(b'x' * 32, 'response', body['request_id'], raw, 'a' * 64, request.headers['x-wordheat-issued-at'])
            return httpx.Response(200, content=raw, headers={'x-wordheat-signature': signature})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url='http://127.0.0.1:8124') as http:
            scorer = module.ScorerClient(b'x' * 32, 'fixture-v1', 'a' * 64, 2, http=http, deadline_ms=20)
            with pytest.raises(module.ScorerFailure) as error:
                await scorer.embeddings(['测试'])
            assert error.value.code == 'SCORER_TIMEOUT'
    asyncio.run(run())

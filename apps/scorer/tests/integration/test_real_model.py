"""Real-model component test. A pass does not establish G1-Q or Windows G1-R."""
import asyncio
import importlib.util
import math
import os
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.mark.real_model
def test_real_offline_encoding_and_authenticated_client():
    manifest_path = os.environ.get('WORDHEAT_TEST_MODEL_MANIFEST')
    model_root = os.environ.get('WORDHEAT_TEST_MODEL_ROOT')
    if not manifest_path or not model_root:
        pytest.skip('NOT_RUN: explicit real model manifest/root not supplied')
    from app.main import create_app
    path = Path(__file__).resolve().parents[3] / 'api/app/modules/scoring/client.py'
    spec = importlib.util.spec_from_file_location('real_scoring_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Synthetic per-test memory key, never saved or a production credential.
    app = create_app(b'component-test-key-not-production', Path(manifest_path), Path(model_root))
    async def run():
        async with app.router.lifespan_context(app):
            assert app.state.ready is True
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8124') as http:
                client = module.ScorerClient(b'component-test-key-not-production', app.state.manifest.manifest_id,
                                             app.state.manifest.sha256, app.state.manifest.dimension, http=http, deadline_ms=10000)
                vectors = await client.embeddings(['苹果', '梨子', '电脑'])
                assert len(vectors) == 3
                assert all(len(v) == app.state.manifest.dimension for v in vectors)
                assert all(all(math.isfinite(x) for x in v) for v in vectors)
                assert all(abs(sum(x*x for x in v) - 1) < .001 for v in vectors)
                assert vectors[0] != vectors[2]
                assert (await client.embeddings(['苹果']))[0] == pytest.approx(vectors[0], abs=1e-6)
                # Synthetic calibration solely exercises formula/transport, not quality acceptance.
                score = await client.score(word='梨子', frozen_puzzle={'manifest_id': app.state.manifest.manifest_id,
                    'word': '苹果', 'calibration': {'b': 0., 'u': 1., 'gamma': 1.}})
                assert 0 <= score <= 99.9
        assert app.state.ready is False
    asyncio.run(run())

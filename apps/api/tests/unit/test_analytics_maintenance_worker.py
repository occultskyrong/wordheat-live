import asyncio
from types import SimpleNamespace
import pytest

def test_worker_runs_startup_then_stops_without_background_work():
    import app.modules.analytics.maintenance as module
    assert hasattr(module,'AnalyticsMaintenanceWorker'), 'awaitable maintenance lifecycle missing'
    calls=[]
    async def scenario():
        service=SimpleNamespace(maintenance_tick=lambda:calls.append('tick'))
        worker=module.AnalyticsMaintenanceWorker(service,interval_seconds=0.01)
        await worker.start(); assert calls==['tick']
        await asyncio.sleep(0.025); await worker.stop(); count=len(calls)
        await asyncio.sleep(0.025); assert len(calls)==count and count>=2
    asyncio.run(scenario())

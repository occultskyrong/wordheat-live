"""Observable complete-file preflight failures and interrupted-download cleanup."""
import errno
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.errors import DomainError
from app.modules.analytics.service import AnalyticsService,VERSION

def payload():
    return dict(metric_definition_version=VERSION,snapshot_id='frozen',as_of={},closure={},coverage={},source_partitions=[],_minutes=[],_rounds=[])

def test_disk_quota_preflight_has_controlled_error_and_no_file(monkeypatch,tmp_path):
    import app.modules.analytics.service as module
    real=module.os.fdopen
    def no_disk(fd,*args,**kwargs):
        raise OSError(errno.ENOSPC,'synthetic disk quota')
    monkeypatch.setattr(module.os,'fdopen',no_disk)
    with pytest.raises(DomainError) as error: AnalyticsService.csv_file(payload())
    assert error.value.code=='ANALYTICS_CAPACITY_BUSY' and error.value.details['reason']=='STORAGE_QUOTA'

def test_private_export_directory_and_crash_orphan_cleanup(tmp_path):
    assert hasattr(AnalyticsService,'cleanup_export_files'), 'crash cleanup for private export files missing'
    path=AnalyticsService.csv_file(payload()); assert path.parent.name.startswith('wordheat-analytics-')
    assert path.parent.stat().st_mode&0o077==0
    os.utime(path,(0,0))
    AnalyticsService.cleanup_export_files()
    assert not path.exists()

def test_csv_size_and_timeout_remove_preflight_file(monkeypatch):
    import app.modules.analytics.service as module
    paths=[]; real=module.tempfile.mkstemp
    def remember(*args,**kwargs):
        fd,path=real(*args,**kwargs); paths.append(Path(path)); return fd,path
    monkeypatch.setattr(module.tempfile,'mkstemp',remember)
    monkeypatch.setattr(module,'MAX_EXPORT_BYTES',1)
    with pytest.raises(DomainError) as error: AnalyticsService.csv_file(payload())
    assert error.value.code=='ANALYTICS_EXPORT_TOO_LARGE' and all(not p.exists() for p in paths)
    monkeypatch.setattr(module,'MAX_EXPORT_BYTES',32*1024*1024)
    ticks=iter([0,31]); monkeypatch.setattr(module.time,'monotonic',lambda:next(ticks))
    with pytest.raises(DomainError) as error: AnalyticsService.csv_file(payload())
    assert error.value.code=='ANALYTICS_EXPORT_TIMEOUT' and all(not p.exists() for p in paths)

def test_database_disk_quota_has_controlled_snapshot_error():
    from contextlib import contextmanager
    import sqlalchemy as sa
    class FullDisk(Exception): sqlstate='53100'
    service=AnalyticsService(SimpleNamespace(),b'x'*32)
    @contextmanager
    def full():
        raise sa.exc.OperationalError('synthetic',{},FullDisk('private driver details'))
        yield
    service.locked=full
    with pytest.raises(DomainError) as error: service.snapshot('session','actor')
    assert error.value.code=='ANALYTICS_CAPACITY_BUSY'
    assert error.value.details['reason']=='STORAGE_QUOTA' and 'private' not in str(error.value)

def test_interrupted_download_unlinks_file_and_releases_contexts():
    import asyncio
    import time
    from contextlib import ExitStack
    from app.modules.analytics.router import ExportResponse
    path=AnalyticsService.csv_file(payload()); stack=ExitStack(); released=[]
    stack.callback(lambda:released.append(True))
    async def scenario():
        async def receive(): return {'type':'http.disconnect'}
        async def send(message): raise ConnectionResetError('synthetic disconnected client')
        response=ExportResponse(path,stack,time.monotonic())
        with pytest.raises(ConnectionResetError):
            await response({'type':'http','method':'GET','path':'/','headers':[],'http_version':'1.1','asgi':{'spec_version':'2.4'}},receive,send)
    asyncio.run(scenario())
    assert released==[True] and not path.exists()

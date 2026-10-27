"""Real PG admission boundary; never holds the slot across scoring."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from test_local_flow import environment, prepared_round


def test_full_slots_reject_before_db_and_release_after_failure(environment, monkeypatch):
    db,actor=environment
    service,body=prepared_round(db,actor,'Ingress pressure')
    assert hasattr(service,'_admission_slots')
    entered=Barrier(5); release=Event(); original=service.command
    def blocked(*args,**kwargs):
        # All four holders really occupy their own PG transaction. No fifth
        # contender may check out a connection or create a rejection event.
        with db.engine.begin() as c:
            c.execute(sa.text('SELECT 1'))
            entered.wait(timeout=5); release.wait(timeout=5)
        raise RuntimeError('controlled admission rollback')
    monkeypatch.setattr(service,'command',blocked)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(service.submit_guess,actor,'slot-'+str(i),body) for i in range(4)]
        entered.wait(timeout=5)
        start=time.monotonic()
        try:
            with pytest.raises(DomainError) as error:service.submit_guess(actor,'overflow',body)
            assert error.value.code=='RATE_LIMITED' and error.value.status_code==429
            assert time.monotonic()-start<0.2
            with db.engine.connect() as c:
                assert c.execute(sa.select(sa.func.count()).select_from(s.idempotency).where(s.idempotency.c.key=='overflow')).scalar_one()==0
                assert c.execute(sa.select(sa.func.count()).select_from(s.rejections).where(s.rejections.c.session_id==body['session_id'])).scalar_one()==0
        finally:release.set()
        for future in futures:
            with pytest.raises(RuntimeError):future.result(timeout=5)
    monkeypatch.setattr(service,'command',original)
    # Every exceptional holder released its slot. Ordinary use case succeeds.
    result=service.submit_guess(actor,'after-failure',body)
    assert result['solved'] is True
    assert all(service._admission_slots.acquire(blocking=False) for _ in range(4))
    for _ in range(4):service._admission_slots.release()


def test_old_key_is_checked_before_stale_round_and_independent_user_flow(environment):
    db,actor=environment
    service,body=prepared_round(db,actor,'Ingress idempotency')
    first=service.submit_guess(actor,'old-admission',body)
    assert first['solved'] is True
    # The exact hit has closed the round; idempotency still returns its result.
    assert service.submit_guess(actor,'old-admission',body)==first
    from app.modules.auth.service import uid
    second_actor=uid()
    with db.engine.begin() as c:
        c.execute(s.operators.insert().values(id=second_actor,username='independent-operator',password_hash='test-fixture-never-authenticated',display_name='Independent operator',permissions=['session.control','puzzle.manage','puzzle.review']))
    other,other_body=prepared_round(db,second_actor,'Independent user')
    other_body=other_body|{'display_name':'Independent audience'}
    assert other.submit_guess(second_actor,'independent',other_body)['solved'] is True


def test_native_path_uses_same_slot_without_wrapping_official(environment,monkeypatch):
    db,actor=environment
    service,body=prepared_round(db,actor,'Native pressure')
    assert hasattr(service,'_admission_slots')
    for _ in range(4):assert service._admission_slots.acquire(blocking=False)
    try:
        with pytest.raises(DomainError) as error:service._submit_guess(actor,'native-full',body,'/internal/rehearsal-input',admission=object())
        assert error.value.code=='RATE_LIMITED'
        monkeypatch.setattr(service,'_submit_guess_in_slot',lambda *args,**kwargs:{'official':'untouched'})
        assert service._submit_guess(actor,'other',body,'/official-input')=={'official':'untouched'}
    finally:
        for _ in range(4):service._admission_slots.release()

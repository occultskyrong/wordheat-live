import importlib
import importlib.util
from uuid import uuid4
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.modules.connectors import models as m
from test_trusted_control import database


def store(database):
    name='app.modules.connectors.store'
    assert importlib.util.find_spec(name) is not None,'Missing durable connector store'
    return importlib.import_module(name).ConnectorStore(database)


def test_persistence_ack_outbox_is_atomic_contiguous_and_hash_conflict_visible(database):
    from pathlib import Path
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'unit'))
    from test_connector_protocol import frame,body
    s=store(database);sid=uuid4().hex
    f1=frame(body()|{'stream_seq':'1'});f2=frame(body())
    conn={k:f1[k] for k in ('lease_id','installation_id','delivery_generation','connection_nonce')}
    with database.engine.begin() as c:
        c.execute(m.bindings.insert().values(id=sid,session_id=sid,connector_id='registered',state='CONNECTED',connection=conn))
        assert s.persist(c,sid,f2,conn)==0
        assert c.execute(sa.select(sa.func.count()).select_from(m.acks).where(m.acks.c.session_id==sid)).scalar_one()==0
        assert s.persist(c,sid,f1,conn)==2
    with database.engine.begin() as c:
        assert s.persist(c,sid,f1,conn)==2
        assert c.execute(sa.select(sa.func.count()).select_from(m.acks).where(m.acks.c.session_id==sid)).scalar_one()==1
        changed=frame(body()|{'stream_seq':'1','payload':{'source_msg_id':'changed'}})
        with pytest.raises(DomainError):s.persist(c,sid,changed,conn)


def test_receipt_outbox_requires_real_source_and_prior_received(database):
    s=store(database);sid=uuid4().hex
    with database.engine.begin() as c:
        c.execute(m.bindings.insert().values(id=sid,session_id=sid,connector_id='registered',state='CONNECTED'))
        with pytest.raises(DomainError):s.receipt(c,sid,'missing',1,'display.received','receipt')

"""Dedicated relay PG: only a freshly owned namespace is created/dropped."""
from datetime import datetime,timezone
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from app.core import safe_connect,validate_database,dsn,RelayService,VerifiedGrant


@pytest.fixture
def relay_environment():
    filename=os.environ.get('RELAY_TEST_ENV_FILE')
    if not filename:pytest.skip('Explicit dedicated relay configuration required')
    values={}
    for line in Path(filename).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key,value=line.split('=',1)
            if key in {'RELAY_DATABASE_URL','RELAY_MIGRATION_DATABASE_URL'}:values[key]=value.strip().strip('"\'')
    runtime=dsn(values['RELAY_DATABASE_URL']);migration=dsn(values['RELAY_MIGRATION_DATABASE_URL'])
    if urlsplit(runtime).path!='/wordheat_relay_test' or urlsplit(migration).path!='/wordheat_relay_test':raise ValueError('Dedicated relay test target required')
    schema='wordheat_relay_pytest_'+uuid4().hex;nonce=uuid4().hex;runtime_role=urlsplit(runtime).username
    with safe_connect(migration,row_factory=dict_row) as c:
        validate_database(c,'wordheat_relay_test','wordheat-live:test:relay',True)
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        c.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(schema),sql.Literal(nonce)))
        for path in sorted((Path(__file__).parents[1]/'migrations').glob('*.sql')):
            text=path.read_text().replace('CREATE SCHEMA IF NOT EXISTS relay;','').replace('relay.',schema+'.')
            c.execute(text)
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(runtime_role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(runtime_role)))
    clock=[datetime.now(timezone.utc)]
    service=RelayService(runtime,schema=schema,clock=lambda:clock[0])
    grant=VerifiedGrant('synthetic-app',uuid4().hex,'synthetic-install','synthetic-session','test_live',clock[0]+__import__('datetime').timedelta(hours=1))
    lease=service.acquire(grant)
    yield service,grant,lease,clock
    with safe_connect(migration,row_factory=dict_row) as c:
        validate_database(c,'wordheat_relay_test','wordheat-live:test:relay',True)
        actual=c.execute('SELECT obj_description(oid) AS marker FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone()
        if not actual or actual['marker']!=nonce:raise ValueError('Refusing cleanup of an unowned namespace')
        c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

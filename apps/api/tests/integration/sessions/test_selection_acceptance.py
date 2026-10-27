"""BE-A19 real-PG acceptance; synthetic fixtures never establish platform readiness."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
import time
import uuid

import pytest
import sqlalchemy as sa

from app.config import Settings, read_fields
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.infrastructure.db.database import Database
from app.modules.auth.service import uid, hash_password, PERMISSIONS
from app.modules.sessions.service import LocalService, RULES
from app.modules.wordbank import selection
from app.modules.wordbank.service import WordbankService


class ReadyScorer:
    def readiness(self):
        return {'ready': True, 'manifest_id': 'selection-fixture-manifest'}


@pytest.fixture(scope='module')
def database():
    path = os.environ.get('WORDHEAT_TEST_CONFIG')
    if not path:
        pytest.fail('Dedicated PG configuration required', pytrace=False)
    fields = read_fields(Path(path), {'WORDHEAT_TEST_DATABASE_URL', 'WORDHEAT_TEST_MIGRATION_DATABASE_URL'})
    db = Database(Settings(fields['WORDHEAT_TEST_DATABASE_URL'], allowed_hosts=('testserver',), allowed_origins=('http://testserver',)))
    db.verify_runtime()
    with db.engine.connect() as c:
        row = c.execute(sa.text("SELECT current_database(), shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")).one()
        assert tuple(row) == ('wordheat_test', 'wordheat-live:test:local'), 'Dedicated test database identity required'
        assert c.execute(sa.select(s.instance.c.instance_id)).scalar_one() == 'wordheat-local-development'
    yield db
    db.engine.dispose()


@pytest.fixture
def case(database):
    suffix = uuid.uuid4().hex
    username = f'selection-{suffix}'
    service = LocalService(database, ReadyScorer(), display_probe=lambda **scope: True)
    with database.engine.begin() as c:
        actor = uid()
        c.execute(s.operators.insert().values(id=actor, username=username, password_hash=hash_password('synthetic-selection-test-password'), display_name='Selection fixture', permissions=PERMISSIONS))
        for name in ('a', 'b'):
            c.execute(s.puzzles.insert().values(id=f'{suffix}-{name}-v1', puzzle_id=f'{suffix}-{name}', word=f'合成{name}', aliases=[], category_id=suffix, difficulty=1, explanation='Synthetic fixture', status='published', manifest_id='selection-fixture-manifest', calibration_id='fixture-calibration', evaluation_id='fixture-evaluation', approved_by=actor))
    fixture = {'db': database, 'service': service, 'actor': actor, 'category': suffix, 'username': username}
    yield fixture
    # Delete only this fixture's records; the shared dedicated test DB is not truncated.
    with database.engine.begin() as c:
        session_ids = list(c.execute(sa.select(s.sessions.c.id).where(s.sessions.c.actor_id == actor)).scalars())
        round_ids = list(c.execute(sa.select(s.rounds.c.id).where(s.rounds.c.session_id.in_(session_ids))).scalars())
        for table in reversed(s.metadata.sorted_tables):
            if table is s.instance or table is s.catalog:
                continue
            if table is s.operators:
                condition = table.c.id == actor
            elif table is s.sessions:
                condition = table.c.id.in_(session_ids)
            elif table is s.puzzles:
                condition = table.c.category_id.in_([suffix, suffix + '-empty'])
            elif 'session_id' in table.c:
                condition = table.c.session_id.in_(session_ids)
            elif 'round_id' in table.c:
                condition = table.c.round_id.in_(round_ids)
            elif 'actor_id' in table.c:
                condition = table.c.actor_id == actor
                if table is s.idempotency:
                    condition = sa.or_(condition, table.c.path.in_([f'/api/v1/live-sessions/{sid}/selection' for sid in session_ids]))
            else:
                continue
            c.execute(table.delete().where(condition))


def session(case, *, active=False, empty=False, auto_next=False):
    with case['db'].engine.begin() as c:
        result = case['service'].create_session(c, case['actor'], {'label': 'Selection acceptance', 'mode': 'rehearsal', 'run_purpose': 'local', 'rules': {'category_id': case['category'] + ('-empty' if empty else ''), 'auto_next': auto_next}})
        sid = result['session_id']
        if active:
            c.execute(s.sessions.update().where(s.sessions.c.id == sid).values(status='ACTIVE'))
            ticket=case['service'].ticket(c,case['actor'],{'session_id':sid,'purpose':'primary'})
    if active: case['service'].exchange(ticket['ticket'])
    return sid


def current(case, sid):
    with case['db'].engine.connect() as c:
        return case['service'].next_selection(c, sid), c.execute(sa.select(s.sessions.c.version).where(s.sessions.c.id == sid)).scalar_one()


def select(case, sid, name=None):
    preview, version = current(case, sid)
    args = {'strategy': 'specific' if name else 'random', 'expected_selection_id': preview['selection_id'], 'expected_selection_version': preview['version']}
    if name:
        args['puzzle_id'] = f"{case['category']}-{name}"
    with case['db'].engine.begin() as c:
        return case['service'].session_command(c, case['actor'], sid, {'command': 'select_next', 'expected_version': version, 'args': args})


def round_body(case, sid, preview=None):
    latest, version = current(case, sid)
    preview = preview or latest
    return {'session_id': sid, 'expected_session_version': version, 'selection_id': preview['selection_id'], 'expected_selection_version': preview['version']}


def create(case, body, key=None, service=None):
    service = service or case['service']
    return service.command(case['actor'], 'POST', '/api/v1/rounds', key or uuid.uuid4().hex, body, lambda c: service.create_round(c, case['actor'], body))


def disable(case, name):
    wordbank = WordbankService(case['service'])
    pid = f"{case['category']}-{name}"
    body = {'command': 'disable', 'expected_version': 1, 'args': {'reason': 'Synthetic acceptance disable'}}
    return wordbank.mutate(case['actor'], 'POST', f'/api/v1/puzzles/{pid}/commands', uuid.uuid4().hex, body, 'puzzle.review', lambda c: wordbank.puzzle_command(c, case['actor'], pid, body))


def test_old_reference_cannot_turn_empty_into_a_fresh_random_intent(case):
    sid = session(case, active=True)
    old = select(case, sid, 'a')['state']
    disable(case, 'a')
    disable(case, 'b')
    assert current(case, sid)[0]['status'] == 'EMPTY'
    # A later eligible catalog row does not authorize the already obsolete intent.
    with case['db'].engine.begin() as c:
        c.execute(s.puzzles.update().where(s.puzzles.c.id == f"{case['category']}-b-v1").values(status='published'))
    before = current(case, sid)
    body = round_body(case, sid, old) | {'selection': {'strategy': 'random'}}
    with pytest.raises(DomainError) as failure:
        create(case, body)
    assert failure.value.code == 'NEXT_SELECTION_STALE'
    assert current(case, sid) == before


def test_same_trigger_cannot_bind_different_selection_content(case):
    sid = session(case)
    first = select(case, sid, 'a')['state']
    with case['db'].engine.begin() as c:
        selection.catalog_lock(c)
        case['service'].session(c, sid)
        case['service'].selection_notice(c, sid, None, first, 'CATALOG_CHANGED', 'fixed-trigger')
    with pytest.raises(DomainError) as failure:
        with case['db'].engine.begin() as c:
            selection.catalog_lock(c)
            case['service'].session(c, sid)
            case['service'].selection_notice(c, sid, None, first | {'version': first['version'] + 1}, 'CATALOG_CHANGED', 'fixed-trigger')
    assert failure.value.code == 'IDEMPOTENCY_KEY_REUSED'


def test_aba_rejects_old_identity_and_revision_independently(case):
    sid = session(case, active=True)
    old = select(case, sid, 'a')['state']
    middle = select(case, sid, 'b')['state']
    latest = select(case, sid, 'a')['state']
    assert old['version'] < middle['version'] < latest['version']
    assert len({old['selection_id'], middle['selection_id'], latest['selection_id']}) == 3
    for identity, revision in ((old['selection_id'], old['version']), (old['selection_id'], latest['version']), (latest['selection_id'], old['version'])):
        body = round_body(case, sid) | {'selection_id': identity, 'expected_selection_version': revision}
        with pytest.raises(DomainError) as failure:
            create(case, body)
        assert failure.value.code == 'NEXT_SELECTION_VERSION_CONFLICT'
    result = create(case, round_body(case, sid))
    with case['db'].engine.connect() as c:
        assert c.execute(sa.select(s.rounds.c.puzzle_version_id).where(s.rounds.c.id == result['round_id'])).scalar_one() == f"{case['category']}-a-v1"


def test_concurrent_select_commands_report_selection_cas_conflict(case):
    sid = session(case)
    barrier = threading.Barrier(2)
    def choose(name):
        body = {'command': 'select_next', 'expected_version': 1, 'args': {'strategy': 'specific', 'puzzle_id': f"{case['category']}-{name}", 'expected_selection_id': None, 'expected_selection_version': 0}}
        barrier.wait(timeout=5)
        try:
            return case['service'].command(case['actor'], 'POST', f'/api/v1/live-sessions/{sid}/commands', f'choose-{name}', body, lambda c: case['service'].session_command(c, case['actor'], sid, body))
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(choose, ('a', 'b')))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert [result for result in results if isinstance(result, str)] == ['NEXT_SELECTION_VERSION_CONFLICT']


def test_disabled_preview_reports_stale_and_never_substitutes_new_preview(case):
    sid = session(case, active=True)
    old = select(case, sid, 'a')['state']
    disable(case, 'a')
    before = current(case, sid)
    with pytest.raises(DomainError) as failure:
        create(case, round_body(case, sid, old))
    assert failure.value.code == 'NEXT_SELECTION_STALE'
    assert current(case, sid) == before


def test_preview_reference_required_and_request_cannot_override_it(case):
    sid = session(case, active=True)
    select(case, sid, 'a')
    for extra, error in (({'selection_id': None, 'expected_selection_version': None}, 'NEXT_SELECTION_VERSION_CONFLICT'), ({'selection': {'strategy': 'random'}}, 'NEXT_SELECTION_MISMATCH')):
        with pytest.raises(DomainError) as failure:
            create(case, round_body(case, sid) | extra)
        assert failure.value.code == error
    body = {'session_id': sid, 'expected_session_version': current(case, sid)[1], 'selection': {'strategy': 'random'}}
    with pytest.raises(DomainError) as failure:
        create(case, body)
    assert failure.value.code == 'NEXT_SELECTION_REQUIRED'


def test_concurrent_consumers_and_restart_idempotency_keep_one_frozen_round(case):
    sid = session(case, active=True)
    select(case, sid, 'a')
    body = round_body(case, sid)
    barrier = threading.Barrier(2)
    def consume(key):
        barrier.wait(timeout=5)
        try:
            return create(case, body, key)
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(consume, ('concurrent-one', 'concurrent-two')))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert [result for result in results if isinstance(result, str)] == ['ROUND_VERSION_CONFLICT']
    winning_key = 'concurrent-one' if isinstance(results[0], dict) else 'concurrent-two'
    after = current(case, sid)
    restarted = LocalService(case['db'], ReadyScorer(), display_probe=lambda **scope: True)
    assert create(case, body, winning_key, restarted) == successes[0]
    assert current(case, sid) == after
    with case['db'].engine.connect() as c:
        rounds = list(c.execute(sa.select(s.rounds).where(s.rounds.c.session_id == sid)).mappings())
        assert len(rounds) == 1 and rounds[0]['puzzle_version_id'] == f"{case['category']}-a-v1"
        consumed = c.execute(sa.select(s.next_selections).where(s.next_selections.c.id == body['selection_id'])).mappings().one()
        assert consumed['status'] == 'CONSUMED' and consumed['consumed_round_id'] == successes[0]['round_id']
        assert current(case, sid)[0]['puzzle_version_id'] == f"{case['category']}-b-v1"


def test_empty_gets_never_reselect_and_new_catalog_trigger_runs_once(case):
    sid = session(case, empty=True)
    select(case, sid)
    before = current(case, sid)
    wordbank = WordbankService(case['service'])
    with case['db'].engine.begin() as c:
        selection.catalog_lock(c)
        for _ in range(5):
            wordbank.refresh_empty(c)
    assert current(case, sid) == before
    with case['db'].engine.begin() as c:
        selection.catalog_lock(c)
        c.execute(s.puzzles.update().where(s.puzzles.c.id == f"{case['category']}-a-v1").values(category_id=case['category'] + '-empty'))
        c.execute(s.catalog.update().where(s.catalog.c.id == 'global').values(revision=s.catalog.c.revision + 1))
        wordbank.refresh_empty(c)
    after = current(case, sid)
    assert after[0]['status'] == 'READY' and after[0]['version'] > before[0]['version']
    with case['db'].engine.begin() as c:
        selection.catalog_lock(c)
        wordbank.refresh_empty(c)
    assert current(case, sid) == after


def test_applied_automatic_trigger_survives_restart_without_random_or_new_event(case):
    sid = session(case)
    select(case, sid, 'a')
    def apply(service, reason='CATALOG_CHANGED', content=None):
        with case['db'].engine.begin() as c:
            selection.catalog_lock(c)
            row = service.session(c, sid)
            return service.reselect(c, row, reason, 'catalog:synthetic-1', content=content or {'catalog_revision': 1})
    applied = apply(case['service'])
    before = current(case, sid)
    with case['db'].engine.connect() as c:
        events_before = list(c.execute(sa.select(s.outbox.c.id).where(s.outbox.c.session_id == sid).order_by(s.outbox.c.seq)).scalars())
    restarted = LocalService(case['db'], ReadyScorer())
    assert apply(restarted) == applied
    assert current(case, sid) == before
    with case['db'].engine.connect() as c:
        assert list(c.execute(sa.select(s.outbox.c.id).where(s.outbox.c.session_id == sid).order_by(s.outbox.c.seq)).scalars()) == events_before
    with pytest.raises(DomainError) as failure:
        apply(restarted, content={'catalog_revision': 2})
    assert failure.value.code == 'IDEMPOTENCY_KEY_REUSED'
    assert current(case, sid) == before


def test_touch_persists_sealed_port_after_counter_and_tolerates_absent_observer(case):
    sid = session(case)
    with case['db'].engine.begin() as c:
        before = c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id == sid)).scalar_one()
        calls = []
        class Observer:
            def persist_sealed(self, connection, session_id):
                calls.append((connection is c, session_id, connection.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id == sid)).scalar_one()))
        case['service'].analytics_observer = Observer()
        case['service'].touch(c, sid)
        assert calls == [(True, sid, before + 1)]
        case['service'].analytics_observer = None
        case['service'].touch(c, sid)
        assert c.execute(sa.select(s.sessions.c.analytics_commit_counter).where(s.sessions.c.id == sid)).scalar_one() == before + 2


def assert_one_hundred_http_private_reads_do_not_write_or_select(case, empty):
    from fastapi.testclient import TestClient
    from app.main import create_app
    sid = session(case, empty=empty)
    select(case, sid, None if empty else 'a')
    app = create_app(case['db'].settings, scorer=ReadyScorer())
    client = TestClient(app, headers={'Origin': 'http://testserver', 'X-Contract-Version': 'LW-C1/1.0.0', 'X-BE-Extension-Version': 'LW-BE/1.1.0'})
    assert client.post('/api/v1/auth/sessions', json={'username': case['username'], 'password': 'synthetic-selection-test-password'}).status_code == 200
    def persisted():
        with case['db'].engine.connect() as c:
            return (dict(c.execute(sa.select(s.sessions).where(s.sessions.c.id == sid)).mappings().one()), list(c.execute(sa.select(s.next_selections).where(s.next_selections.c.session_id == sid)).mappings()), list(c.execute(sa.select(s.outbox).where(s.outbox.c.session_id == sid)).mappings()))
    before = persisted()
    expected = current(case, sid)[0]
    for _ in range(100):
        for path in (f'/api/v1/live-sessions/{sid}', f'/api/v1/studio/sessions/{sid}/snapshot'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()['data']['next_selection'] == expected
    assert persisted() == before
    with case['db'].engine.begin() as c:
        ticket = case['service'].ticket(c, case['actor'], {'session_id': sid, 'purpose': 'primary'})
    display = TestClient(app, headers={'Origin': 'http://testserver', 'X-BE-Extension-Version': 'LW-BE/1.1.0'})
    assert display.post('/api/v1/display-sessions', json={'ticket': ticket['ticket']}).status_code == 200
    public = display.get(f'/api/v1/display/sessions/{sid}/snapshot')
    assert public.status_code == 200
    serialized = public.text
    for forbidden in ('next_selection', expected['selection_id'], expected['puzzle_version_id'], expected['answer']):
        if forbidden:
            assert forbidden not in serialized
    assert display.get(f'/api/v1/studio/sessions/{sid}/snapshot').status_code == 401
    with case['db'].engine.begin() as c:
        c.execute(s.operators.update().where(s.operators.c.id == case['actor']).values(permissions=[]))
    try:
        assert client.get(f'/api/v1/live-sessions/{sid}').status_code == 403
        assert client.get(f'/api/v1/studio/sessions/{sid}/snapshot').status_code == 403
    finally:
        with case['db'].engine.begin() as c:
            c.execute(s.operators.update().where(s.operators.c.id == case['actor']).values(permissions=PERMISSIONS))
        client.close()
        display.close()
        app.state.database.engine.dispose()


def test_one_hundred_http_ready_reads_do_not_write_or_select(case):
    assert_one_hundred_http_private_reads_do_not_write_or_select(case, empty=False)


def test_one_hundred_http_empty_reads_do_not_write_or_select(case):
    assert_one_hundred_http_private_reads_do_not_write_or_select(case, empty=True)


def test_configure_and_disable_without_rounds_emit_durable_studio_notices(case):
    from app.modules.realtime.service import RealtimeService
    sid = session(case)
    old = select(case, sid, 'a')['state']
    with case['db'].engine.begin() as c:
        c.execute(s.puzzles.update().where(s.puzzles.c.id == f"{case['category']}-b-v1").values(difficulty=2))
    rules = RULES | {'category_id': case['category'], 'difficulty_min': 2, 'difficulty_max': 2, 'auto_next': False}
    with case['db'].engine.begin() as c:
        configured = case['service'].session_command(c, case['actor'], sid, {'command': 'configure', 'expected_version': current(case, sid)[1], 'args': {'apply': 'next_round', 'next_rules': rules}})
    assert current(case, sid)[0]['puzzle_version_id'] == f"{case['category']}-b-v1"
    disable(case, 'b')
    assert current(case, sid)[0]['status'] == 'EMPTY'
    with case['db'].engine.connect() as c:
        commands = list(c.execute(sa.select(s.commands).where(s.commands.c.session_id == sid, s.commands.c.actor_id == 'system:selection').order_by(s.commands.c.created_at)).mappings())
        events = list(c.execute(sa.select(s.outbox).where(s.outbox.c.session_id == sid).order_by(s.outbox.c.seq)).mappings())
        assert c.execute(sa.select(sa.func.count()).select_from(s.rounds).where(s.rounds.c.session_id == sid)).scalar_one() == 0
        history = c.execute(sa.select(s.next_selections).where(s.next_selections.c.id == old['selection_id'])).mappings().one()
        assert history['status'] == 'INVALIDATED' and history['reason_code'] == 'NEXT_RULES_CHANGED'
    assert [command['response']['state']['reason'] for command in commands] == ['OPERATOR_SELECTION', 'NEXT_RULES_CHANGED', 'PUZZLE_DISABLED']
    assert len(events) == len(commands) and all(event['type'] == 'command.updated' and event['audience'] == 'STUDIO' for event in events)
    assert commands[1]['response']['resource_version'] == configured['resource_version']
    for event in events:
        assert '合成' not in str(event['payload']) and 'aliases' not in str(event['payload'])
    restarted = RealtimeService(LocalService(case['db'], ReadyScorer()))
    first, _ = restarted.replay(sid, 'STUDIO', 0, case['actor'])
    second, _ = restarted.replay(sid, 'STUDIO', 0, case['actor'])
    assert [event['id'] for event in first] == [event['id'] for event in second] == [event['id'] for event in events]


def test_automatic_trigger_rollback_retries_once_with_atomic_notice(case):
    sid = session(case)
    select(case, sid, 'a')
    before = current(case, sid)
    with pytest.raises(RuntimeError, match='synthetic rollback'):
        with case['db'].engine.begin() as c:
            selection.catalog_lock(c)
            row = case['service'].session(c, sid)
            case['service'].reselect(c, row, 'CATALOG_CHANGED', 'rollback-trigger', content={'revision': 1})
            raise RuntimeError('synthetic rollback')
    assert current(case, sid) == before
    with case['db'].engine.connect() as c:
        assert not c.execute(sa.select(s.commands.c.id).where(s.commands.c.trigger_key == f'{sid}:CATALOG_CHANGED:rollback-trigger')).first()
    with case['db'].engine.begin() as c:
        selection.catalog_lock(c)
        row = case['service'].session(c, sid)
        applied = case['service'].reselect(c, row, 'CATALOG_CHANGED', 'rollback-trigger', content={'revision': 1})
    assert current(case, sid)[0]['selection_id'] == applied['selection_id']
    with case['db'].engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(s.commands).where(s.commands.c.trigger_key == f'{sid}:CATALOG_CHANGED:rollback-trigger')).scalar_one() == 1


def assert_disable_consume_serial_outcome(case, first):
    sid = session(case, active=True)
    old = select(case, sid, 'a')['state']
    if first == 'consume':
        result = create(case, round_body(case, sid, old))
        disable(case, 'a')
        with case['db'].engine.connect() as c:
            frozen = c.execute(sa.select(s.rounds.c.frozen_puzzle).where(s.rounds.c.id == result['round_id'])).scalar_one()
        assert frozen['word'] == '合成a'
    else:
        disable(case, 'a')
        body = round_body(case, sid, old)
        with pytest.raises(DomainError) as failure:
            create(case, body)
        assert failure.value.code == 'NEXT_SELECTION_STALE'
        assert current(case, sid)[0]['puzzle_version_id'] == f"{case['category']}-b-v1"


def test_consume_before_disable_keeps_frozen_preview(case):
    assert_disable_consume_serial_outcome(case, first='consume')


def test_disable_before_consume_rejects_old_preview(case):
    assert_disable_consume_serial_outcome(case, first='disable')


def test_disable_consume_and_sorted_configure_batch_share_one_global_lock_order(case):
    ids = [session(case, active=True) for _ in range(3)]
    for sid in ids:
        select(case, sid, 'a')
    bodies = {sid: round_body(case, sid) for sid in ids}
    barrier = threading.Barrier(3)
    traces = []
    traces_lock = threading.Lock()
    def begin(connection):
        connection.info['selection_acceptance_locks'] = []
    def before_execute(connection, cursor, statement, parameters, context, many):
        if 'FOR UPDATE' in statement:
            with traces_lock:
                connection.info['selection_acceptance_locks'].append(statement)
    def committed(connection):
        with traces_lock:
            locks = connection.info.get('selection_acceptance_locks', [])
            if locks:
                traces.append(list(locks))
    for name, callback in [('begin', begin), ('before_cursor_execute', before_execute), ('commit', committed), ('rollback', committed)]:
        sa.event.listen(case['db'].engine, name, callback)
    def bounded(c):
        c.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
        c.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
    def operation(kind):
        barrier.wait(timeout=5)
        started = time.monotonic()
        try:
            if kind == 'consume':
                with case['db'].engine.begin() as c:
                    bounded(c)
                    result = case['service'].create_round(c, case['actor'], bodies[ids[0]])
            elif kind == 'disable':
                with case['db'].engine.begin() as c:
                    bounded(c)
                    result = WordbankService(case['service']).puzzle_command(c, case['actor'], f"{case['category']}-a", {'command': 'disable', 'expected_version': 1, 'args': {'reason': 'concurrency fixture'}})
            else:
                with case['db'].engine.begin() as c:
                    bounded(c)
                    result = []
                    for sid in sorted(ids):
                        rules = RULES | {'category_id': case['category'], 'auto_next': False}
                        result.append(case['service'].session_command(c, case['actor'], sid, {'command': 'configure', 'expected_version': bodies[sid]['expected_session_version'], 'args': {'apply': 'next_round', 'next_rules': rules}}))
            return kind, result, time.monotonic() - started
        except DomainError as error:
            return kind, error.code, time.monotonic() - started
    try:
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(operation, ('consume', 'disable', 'configure')))
    finally:
        for name, callback in [('begin', begin), ('before_cursor_execute', before_execute), ('commit', committed), ('rollback', committed)]:
            sa.event.remove(case['db'].engine, name, callback)
    assert len(results) == 3 and all(elapsed < 6 for _, _, elapsed in results)
    assert all(result == 'ROUND_VERSION_CONFLICT' for _, result, _ in results if isinstance(result, str))
    assert traces and all('wordbank_catalog' in trace[0] for trace in traces)
    # The catalog-wide mutation's actual row-lock query sorts every session.
    assert any('ORDER BY live_sessions.id' in statement for trace in traces for statement in trace)
    with case['db'].engine.connect() as c:
        rounds = list(c.execute(sa.select(s.rounds).where(s.rounds.c.session_id.in_(ids))).mappings())
        assert len(rounds) <= 1
        if rounds:
            assert rounds[0]['frozen_puzzle']['word'] == '合成a'
        for sid in ids:
            projection = case['service'].next_selection(c, sid)
            assert projection['puzzle_version_id'] != f"{case['category']}-a-v1"


def test_worker_empty_only_rechecks_for_new_round_or_catalog_trigger(case):
    from app.modules.rounds.worker import LocalRoundWorker
    sid = session(case, active=True, auto_next=True)
    # One published logical puzzle leaves the next pool EMPTY while it is active.
    with case['db'].engine.begin() as c:
        c.execute(s.puzzles.update().where(s.puzzles.c.id == f"{case['category']}-b-v1").values(status='disabled'))
    select(case, sid, 'a')
    result = create(case, round_body(case, sid))
    assert current(case, sid)[0]['status'] == 'EMPTY'
    with case['db'].engine.begin() as c:
        row = case['service'].session(c, sid)
        active = c.execute(sa.select(s.rounds).where(s.rounds.c.id == result['round_id']).with_for_update()).mappings().one()
        case['service'].settle(c, row, active, 'TIMEOUT', None, None)
        # Eligibility became empty before this round's explicit completion trigger.
        c.execute(s.puzzles.update().where(s.puzzles.c.id == f"{case['category']}-a-v1").values(status='disabled'))
        c.execute(s.rounds.update().where(s.rounds.c.id == result['round_id']).values(next_due_ns=0))
    worker = LocalRoundWorker(case['service'])
    worker.schedule_next()
    first = current(case, sid)
    assert first[0]['status'] == 'EMPTY'
    for _ in range(3):
        worker.next_schedule_check = 0
        worker.schedule_next()
    assert current(case, sid) == first
    with case['db'].engine.begin() as c:
        c.execute(s.catalog.update().where(s.catalog.c.id == 'global').values(revision=s.catalog.c.revision + 1))
    worker.next_schedule_check = 0
    worker.schedule_next()
    second = current(case, sid)
    assert second[0]['status'] == 'EMPTY' and second[0]['version'] > first[0]['version']
    worker.next_schedule_check = 0
    worker.schedule_next()
    assert current(case, sid) == second
    with case['db'].engine.connect() as c:
        assert c.execute(sa.select(s.rounds.c.status).where(s.rounds.c.id == result['round_id'])).scalar_one() == 'INTERMISSION'
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id == sid)).scalar_one() == 'ACTIVE'
        assert c.execute(sa.select(sa.func.count()).select_from(s.rounds).where(s.rounds.c.session_id == sid)).scalar_one() == 1
    # A process restart still sees the durable attempted trigger. Cross-boot
    # monotonic scheduling correctly requires recovery without a selection draw.
    restart = LocalRoundWorker(LocalService(case['db'], ReadyScorer(), display_probe=lambda **scope: True))
    restart.schedule_next()
    assert current(case, sid)[0] == second[0]
    with case['db'].engine.connect() as c:
        assert c.execute(sa.select(s.sessions.c.status).where(s.sessions.c.id == sid)).scalar_one() == 'RECOVERY_REQUIRED'

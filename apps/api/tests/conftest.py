"""Pytest-only PostgreSQL namespaces; application configuration stays unchanged.

Each pytest process owns one random schema. Packaged SQL is applied verbatim in
that schema; this deliberately does not exercise production migrate()'s public
GRANT statements. Identity and packaged revision checks remain real.
"""
from contextlib import contextmanager
import hashlib
from importlib.resources import files
import os
from pathlib import Path
import re
import secrets

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

from app.config import read_fields, validate_dsn
from app.infrastructure.db import schema as s
from app.infrastructure.db.verification import verify_target, verify_revisions


class PostgresIsolation:
    def __init__(self, path):
        fields = read_fields(Path(path), {
            'WORDHEAT_TEST_DATABASE_URL', 'WORDHEAT_TEST_MIGRATION_DATABASE_URL',
        })
        self.runtime = make_url(validate_dsn(fields['WORDHEAT_TEST_DATABASE_URL']))
        self.migration = make_url(validate_dsn(fields['WORDHEAT_TEST_MIGRATION_DATABASE_URL']))
        if self.runtime.database != 'wordheat_test' or self.migration.database != 'wordheat_test':
            raise ValueError('Only the explicit WordHeat test database may be isolated')
        if self.runtime.username == self.migration.username:
            raise ValueError('Separate test runtime and migration roles required')
        self.admin = sa.create_engine(self.migration, hide_parameters=True)
        self.schema = None
        self.owned = {}
        self.engines = set()
        self.sentinel = 'pytest_guard_' + secrets.token_hex(16)
        self.sentinel_created = False
        self.listening = False
        self.patches = pytest.MonkeyPatch()

    def matches(self, url):
        try:
            parsed = make_url(url)
            if parsed.drivername not in {'postgresql', 'postgresql+psycopg'} or parsed.query:
                return False
            return parsed.set(drivername='postgresql+psycopg') in {self.runtime, self.migration}
        except (ValueError, TypeError, sa.exc.ArgumentError):
            return False

    def verify_identity(self, connection):
        verify_target(connection)
        row = connection.execute(sa.text("SELECT current_database(), shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()" )).one()
        if tuple(row) != ('wordheat_test', 'wordheat-live:test:local'):
            raise ValueError('Dedicated test database identity mismatch')
        marker = connection.execute(sa.text('SELECT instance_id FROM public.service_instance')).scalar_one()
        if marker != 'wordheat-local-development':
            raise ValueError('Test installation identity mismatch')

    def connect(self, connection):
        if connection.engine is self.admin or not self.matches(connection.engine.url):
            return
        self.engines.add(connection.engine)
        # DBAPI-only SET avoids starting a SQLAlchemy transaction in the
        # engine_connect event; this also reapplies the boundary on pool checkout.
        raw = connection.connection.dbapi_connection
        previous = raw.autocommit
        try:
            raw.autocommit = True
            with raw.cursor() as cursor:
                cursor.execute('SELECT set_config(%s, %s, false)', ('search_path', self.schema))
        finally:
            raw.autocommit = previous

    def new_schema(self):
        name = 'wordheat_pytest_' + secrets.token_hex(16)
        nonce = 'wordheat-live:pytest:' + secrets.token_hex(32)
        with self.admin.begin() as connection:
            self.verify_identity(connection)
            owner = connection.execute(sa.text('SELECT current_user')).scalar_one()
            role = connection.dialect.identifier_preparer.quote(self.runtime.username)
            connection.exec_driver_sql(f'CREATE SCHEMA {name}')
            connection.execute(sa.text(f"COMMENT ON SCHEMA {name} IS '{nonce}'"))
            # The transaction makes registration, SQL and grants atomic. Record
            # ownership after commit only, so a failed initializer cannot leak.
            connection.execute(sa.text("SELECT set_config('search_path', :name, true)"), {'name': name})
            connection.exec_driver_sql('CREATE TABLE wordheat_schema_revisions (revision text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
            for resource in sorted(files('migrations.versions').iterdir(), key=lambda item: item.name):
                if not resource.name.endswith('.sql'):
                    continue
                sql = resource.read_text()
                if re.search(r'\bpublic\b|\bsearch_path\b|\b(?:GRANT|REVOKE)\b|\b(?:CREATE|ALTER|DROP)\s+(?:SCHEMA|ROLE|DATABASE|EXTENSION)\b', sql, re.IGNORECASE):
                    raise ValueError('Packaged SQL exceeds the private test schema boundary')
                connection.exec_driver_sql(sql)
                connection.execute(sa.text('INSERT INTO wordheat_schema_revisions(revision,sha256) VALUES(:revision,:checksum)'), {
                    'revision': resource.name, 'checksum': hashlib.sha256(sql.encode()).hexdigest(),
                })
            connection.execute(s.instance.insert().values(id='local', instance_id='wordheat-local-development', schema_version=1))
            connection.execute(s.catalog.insert().values(id='global', revision=0))
            connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA {name} TO {role}')
            connection.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {name} TO {role}')
            connection.exec_driver_sql(f'REVOKE INSERT, UPDATE, DELETE ON wordheat_schema_revisions, service_instance FROM {role}')
            verify_revisions(connection)
            can_create = connection.execute(sa.text('SELECT has_schema_privilege(:role,:schema,\'CREATE\')'), {'role': self.runtime.username, 'schema': name}).scalar_one()
            unsafe = connection.execute(sa.text("SELECT rolsuper OR rolcreatedb OR has_schema_privilege(rolname,'public','CREATE') FROM pg_roles WHERE rolname=:role"), {'role': self.runtime.username}).scalar_one()
            if can_create or unsafe:
                raise ValueError('Test runtime role must not have DDL privileges')
        self.owned[name] = (owner, nonce)
        return name

    def drop_schema(self, name):
        identity = self.owned.get(name)
        if identity is None:
            raise ValueError('Refusing cleanup of an unowned test schema')
        with self.admin.begin() as connection:
            self.verify_identity(connection)
            actual = connection.execute(sa.text("SELECT pg_get_userbyid(nspowner), obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=:name"), {'name': name}).one_or_none()
            if actual is None or tuple(actual) != identity:
                raise ValueError('Test schema ownership marker mismatch')
            connection.exec_driver_sql(f'DROP SCHEMA {name} CASCADE')
        del self.owned[name]

    @contextmanager
    def temporary_schema(self):
        name = self.new_schema()
        try:
            yield name
        finally:
            self.drop_schema(name)

    def sentinel_exists(self):
        with self.admin.connect() as connection:
            self.verify_identity(connection)
            return connection.execute(sa.text('SELECT count(*) FROM public.operators WHERE id=:id AND username=:id AND password_hash=:id'), {'id': self.sentinel}).scalar_one() == 1

    def migrate(self, dsn, instance_id, runtime_role):
        if not self.matches(dsn):
            return self.original_migrate(dsn, instance_id, runtime_role)
        if make_url(validate_dsn(dsn)) != self.migration or instance_id != 'wordheat-local-development' or runtime_role != self.runtime.username:
            raise ValueError('Isolated test migration identity mismatch')
        engine = sa.create_engine(dsn, hide_parameters=True)
        try:
            with engine.connect() as connection:
                self.verify_identity(connection)
                if connection.execute(sa.select(s.instance.c.instance_id)).scalar_one() != instance_id:
                    raise ValueError('Isolated installation identity mismatch')
                if connection.execute(sa.text('SELECT current_user')).scalar_one() == runtime_role:
                    raise ValueError('Migration and runtime roles must differ')
                verify_revisions(connection)
        finally:
            engine.dispose()

    def start(self):
        try:
            self.schema = self.new_schema()
            with self.admin.begin() as connection:
                self.verify_identity(connection)
                connection.execute(sa.text("INSERT INTO public.operators(id,username,password_hash,display_name,permissions) VALUES(:id,:id,:id,'pytest owned sentinel','[]'::jsonb)"), {'id': self.sentinel})
            self.sentinel_created = True
            sa.event.listen(Engine, 'engine_connect', self.connect)
            self.listening = True
            import app.infrastructure.db.migrate as migration_module
            self.original_migrate = migration_module.migrate
            self.patches.setattr(migration_module, 'migrate', self.migrate)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.listening:
            sa.event.remove(Engine, 'engine_connect', self.connect)
            self.listening = False
        self.patches.undo()
        for engine in self.engines:
            engine.dispose()
        try:
            if self.sentinel_created:
                if not self.sentinel_exists():
                    raise AssertionError('Public owned sentinel was changed by isolated tests')
                with self.admin.begin() as connection:
                    self.verify_identity(connection)
                    connection.execute(sa.text('DELETE FROM public.operators WHERE id=:id AND username=:id AND password_hash=:id'), {'id': self.sentinel})
                self.sentinel_created = False
        finally:
            try:
                for name in list(self.owned):
                    self.drop_schema(name)
            finally:
                self.admin.dispose()


_isolation = None


def pytest_configure(config):
    global _isolation
    path = os.environ.get('WORDHEAT_TEST_CONFIG')
    if path and not config.option.collectonly:
        _isolation = PostgresIsolation(path)
        _isolation.start()


def pytest_unconfigure(config):
    global _isolation
    if _isolation is not None:
        try:
            _isolation.close()
        finally:
            _isolation = None


@pytest.fixture(scope='session')
def postgres_isolation():
    if _isolation is None:
        pytest.skip('Explicit dedicated PostgreSQL test configuration required')
    return _isolation

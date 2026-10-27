"""Real-PG regression checks for the pytest-only owned schema boundary."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine as direct_engine_alias
from app.config import read_fields, validate_dsn
from app.infrastructure.db.database import engine_for as engine_alias


@pytest.mark.parametrize("field", ["WORDHEAT_TEST_DATABASE_URL", "WORDHEAT_TEST_MIGRATION_DATABASE_URL"])
@pytest.mark.parametrize("factory", [direct_engine_alias, engine_alias], ids=["direct-alias", "engine-for-alias"])
def test_matching_connections_use_private_schema(field, factory, postgres_isolation):
    config = os.environ.get("WORDHEAT_TEST_CONFIG")
    if not config:
        pytest.skip("Explicit dedicated PostgreSQL test configuration required")
    fields = read_fields(Path(config), {"WORDHEAT_TEST_DATABASE_URL", "WORDHEAT_TEST_MIGRATION_DATABASE_URL"})
    engine = factory(validate_dsn(fields[field]))
    try:
        with engine.connect() as connection:
            schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
            assert schema and schema.startswith("wordheat_pytest_"), "Test connection escaped its private schema"
            path = connection.execute(sa.text("SHOW search_path")).scalar_one()
            assert "public" not in path, "Test search_path must not fall back to public"
    finally:
        engine.dispose()


def test_concurrent_connections_and_runtime_ddl_boundary(postgres_isolation):
    isolation = postgres_isolation
    engine = direct_engine_alias(isolation.runtime, hide_parameters=True)
    def schema(_):
        with engine.connect() as connection:
            return connection.execute(sa.text('SELECT current_schema()')).scalar_one()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(schema, range(12))) == [isolation.schema] * 12
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE wordheat_runtime_forbidden(id int)')
    finally:
        engine.dispose()


def test_private_table_cleanup_preserves_public_owned_sentinel(postgres_isolation):
    from app.infrastructure.db import schema as s
    isolation = postgres_isolation
    assert isolation.sentinel_exists()
    engine = engine_alias(isolation.runtime)
    try:
        with engine.begin() as connection:
            connection.execute(s.operators.insert().values(id=isolation.sentinel, username=isolation.sentinel, password_hash='private-test', display_name='Private marker'))
            for table in reversed(s.metadata.sorted_tables):
                if table.name not in {'service_instance', 'wordbank_catalog'}:
                    connection.execute(table.delete())
            assert connection.execute(sa.select(sa.func.count()).select_from(s.operators)).scalar_one() == 0
        assert isolation.sentinel_exists(), 'Private table cleanup changed the public owned sentinel'
    finally:
        engine.dispose()


def test_owned_schema_cleanup_on_failure_and_foreign_schema_refusal(postgres_isolation):
    from conftest import PostgresIsolation
    isolation = postgres_isolation
    foreign = PostgresIsolation.__new__(PostgresIsolation)
    foreign.owned = {}
    with pytest.raises(ValueError, match='unowned'):
        foreign.drop_schema(isolation.schema)
    with pytest.raises(RuntimeError, match='injected failure'):
        with isolation.temporary_schema() as name:
            with isolation.admin.begin() as connection:
                connection.exec_driver_sql(f'CREATE TABLE {name}.owned_cleanup_probe(id int)')
            raise RuntimeError('injected failure')
    assert name not in isolation.owned
    with isolation.admin.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM pg_namespace WHERE nspname=:name'), {'name': name}).scalar_one() == 0
    assert isolation.sentinel_exists()


def test_cleanup_checks_actual_database_comment(postgres_isolation):
    isolation = postgres_isolation
    name = isolation.new_schema()
    owner, nonce = isolation.owned[name]
    try:
        with isolation.admin.begin() as connection:
            connection.exec_driver_sql(f"COMMENT ON SCHEMA {name} IS 'changed-owned-test-marker'")
        with pytest.raises(ValueError, match='ownership marker mismatch'):
            isolation.drop_schema(name)
        with isolation.admin.connect() as connection:
            assert connection.execute(sa.text('SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=:name'), {'name': name}).scalar_one() == owner
    finally:
        with isolation.admin.begin() as connection:
            connection.exec_driver_sql(f"COMMENT ON SCHEMA {name} IS '{nonce}'")
        isolation.drop_schema(name)


def test_private_migration_and_runtime_keep_checksum_checks(postgres_isolation):
    from app.config import Settings
    from app.errors import DomainError
    from app.infrastructure.db.database import Database
    isolation = postgres_isolation
    engine = engine_alias(isolation.migration)
    db = Database(Settings(isolation.runtime))
    try:
        with engine.begin() as connection:
            revision, checksum = connection.execute(sa.text('SELECT revision,sha256 FROM wordheat_schema_revisions ORDER BY revision LIMIT 1')).one()
            connection.execute(sa.text('UPDATE wordheat_schema_revisions SET sha256=:hash WHERE revision=:revision'), {'hash': 'changed-private-test-checksum', 'revision': revision})
        try:
            with pytest.raises(DomainError) as error:
                db.verify_runtime()
            assert error.value.code == 'DATABASE_SCHEMA_UNAVAILABLE'
            with pytest.raises(DomainError) as error:
                isolation.migrate(isolation.migration, 'wordheat-local-development', isolation.runtime.username)
            assert error.value.code == 'DATABASE_SCHEMA_UNAVAILABLE'
        finally:
            with engine.begin() as connection:
                connection.execute(sa.text('UPDATE wordheat_schema_revisions SET sha256=:hash WHERE revision=:revision'), {'hash': checksum, 'revision': revision})
        db.verify_runtime()
    finally:
        engine.dispose()
        db.engine.dispose()

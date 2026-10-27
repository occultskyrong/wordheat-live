"""Applies packaged, checksummed SQL revisions using a separate migration role."""
import hashlib
from importlib.resources import files
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from app.errors import DomainError
from app.config import validate_dsn
from app.infrastructure.db.database import engine_for
from app.infrastructure.db import schema as s
from app.infrastructure.db.verification import verify_target,verify_revisions


def migrate(dsn: str, instance_id: str, runtime_role: str):
    engine = engine_for(validate_dsn(dsn))
    if not runtime_role.startswith('wordheat_') or not runtime_role.replace('_','').isalnum():
        raise ValueError('Invalid dedicated runtime role')
    try:
        with engine.begin() as connection:
            verify_target(connection)
            # Existing installations must identify themselves before CREATE/ALTER.
            present=connection.execute(sa.text("SELECT to_regclass('public.service_instance')")).scalar_one()
            if present:
                marker=connection.execute(sa.select(s.instance)).mappings().first()
                if not marker or marker['instance_id']!=instance_id: raise ValueError('Database instance mismatch')
            role = connection.execute(sa.text('SELECT current_user')).scalar_one()
            if role == runtime_role:
                raise ValueError('Migration and runtime roles must differ')
            connection.execute(sa.text('SELECT pg_advisory_xact_lock(674219384)'))
            connection.execute(sa.text('CREATE TABLE IF NOT EXISTS wordheat_schema_revisions (revision text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())'))
            resources=files('migrations.versions')
            for resource in sorted(resources.iterdir(),key=lambda item:item.name):
                if not resource.name.endswith('.sql'): continue
                sql=resource.read_text(); checksum=hashlib.sha256(sql.encode()).hexdigest()
                old=connection.execute(sa.text('SELECT sha256 FROM wordheat_schema_revisions WHERE revision=:revision'),{'revision':resource.name}).scalar_one_or_none()
                if old:
                    if old!=checksum: raise ValueError('Migration checksum mismatch')
                    continue
                connection.exec_driver_sql(sql)
                connection.execute(sa.text('INSERT INTO wordheat_schema_revisions(revision,sha256) VALUES(:revision,:checksum)'),{'revision':resource.name,'checksum':checksum})
            marker = connection.execute(sa.select(s.instance)).mappings().first()
            if marker and marker['instance_id'] != instance_id:
                raise ValueError('Database instance mismatch')
            if not marker:
                connection.execute(s.instance.insert().values(id='local', instance_id=instance_id, schema_version=1))
                connection.execute(s.catalog.insert().values(id='global', revision=0))
            quoted = connection.dialect.identifier_preparer.quote(runtime_role)
            connection.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO {quoted}'))
            connection.execute(sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted}'))
            connection.execute(sa.text(f'REVOKE INSERT, UPDATE, DELETE ON wordheat_schema_revisions, service_instance FROM {quoted}'))
            verify_revisions(connection)
    except SQLAlchemyError:
        raise DomainError('DEPENDENCY_UNAVAILABLE','Local migration operation failed',503) from None
    finally:
        engine.dispose()

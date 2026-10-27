import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.infrastructure.db.verification import verify_target,verify_revisions


def engine_for(dsn):
    return sa.create_engine(dsn, pool_pre_ping=True, hide_parameters=True)


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.engine = engine_for(settings.database_url)
        self.test_namespace=None
        if getattr(settings,'test_namespace_file',None) is not None:
            from app.infrastructure.db.test_namespace import install
            self.test_namespace=install(self.engine,settings)

    def verify_runtime(self):
        try:
            with self.engine.connect() as connection:
                self.verify_connection(connection)
                role = connection.execute(sa.text("SELECT rolsuper, rolcreatedb, has_schema_privilege(current_user,'public','CREATE') AS can_ddl FROM pg_roles WHERE rolname=current_user")).mappings().one()
                if role['rolsuper'] or role['rolcreatedb'] or role['can_ddl']:
                    raise DomainError('DATABASE_ROLE_UNSAFE', 'Runtime role must not have DDL privileges', 503)
                marker = connection.execute(sa.select(s.instance)).mappings().one()
                if marker['instance_id'] != self.settings.instance_id or marker['schema_version'] != 1:
                    raise DomainError('INSTALLATION_MISMATCH', 'Database instance mismatch', 409)
        except SQLAlchemyError:
            raise DomainError('DEPENDENCY_UNAVAILABLE', 'Local database unavailable', 503, True) from None

    def verify_connection(self,connection):
        verify_target(connection)
        verify_revisions(connection)

    def read_snapshot(self):
        """Consistent read-only transaction for atomic GET projections.

        All statements see one MVCC snapshot, so a snapshot's as_of_seq and its
        projections stay consistent, and the read takes no row lock that would
        block a live admission or settlement. READ ONLY makes an accidental write
        fail loudly instead of being silently rolled back with the read.
        Advisory locks taken by official grant verification are still allowed.
        """
        return self.engine.connect().execution_options(isolation_level='REPEATABLE READ',postgresql_readonly=True)

"""Installed-package-only forward schema migration through fixed registered PG tools."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.modules.operations.runtime_postgres import PostgresTools
from test_runtime_backup import tools

RUNTIME='20261026-runtime-control.sql'
DRAIN='20261025-local-drain.sql'
TABLES=['runtime_maintenance_steps','runtime_native_requests','runtime_scope_recoveries','runtime_backups','runtime_commands','runtime_instances']

def migration_tools(isolation):
    value=tools();value.databases['local']=replace(value.databases['local'],runtime_role=isolation.runtime.username)
    return value

def reset_runtime(isolation,schema):
    with isolation.admin.begin() as c:
        for table in TABLES:c.exec_driver_sql('DROP TABLE '+schema+'.'+table+' CASCADE')
        c.execute(sa.text('DELETE FROM '+schema+'.wordheat_schema_revisions WHERE revision IN (:runtime,:drain)'),{'runtime':RUNTIME,'drain':DRAIN})

def apply(value,isolation,schema,version=1):
    return value.migrate_installed('local',schema=schema,namespace_marker=isolation.owned[schema][1],installation_id='wordheat-local-development',target_schema_version=version)

def test_real_append_missing_installed_revisions_then_noop_and_runtime_role_isolation(postgres_isolation):
    isolation=postgres_isolation;value=migration_tools(isolation)
    with isolation.temporary_schema() as schema:
        reset_runtime(isolation,schema)
        proof=apply(value,isolation,schema)
        assert proof['applied_revisions']==[DRAIN,RUNTIME] and proof['noop'] is False and proof['schema_version']==1
        again=apply(value,isolation,schema)
        assert again['noop'] is True and again['applied_revisions']==[] and again['package_sha256']==proof['package_sha256']
        with isolation.admin.connect() as c:
            role=isolation.runtime.username
            assert not c.execute(sa.text("SELECT has_schema_privilege(:role,:schema,'CREATE')"),{'role':role,'schema':schema}).scalar_one()
            assert c.execute(sa.text("SELECT has_table_privilege(:role,:table,'INSERT')"),{'role':role,'table':schema+'.runtime_commands'}).scalar_one()
            for table in ['wordheat_schema_revisions','service_instance']:
                assert not c.execute(sa.text("SELECT has_table_privilege(:role,:table,'UPDATE')"),{'role':role,'table':schema+'.'+table}).scalar_one()

@pytest.mark.parametrize('mutation',['checksum','unknown_revision','future_target'])
def test_tampered_or_uninstalled_migration_is_rejected_without_ddl(postgres_isolation,mutation):
    isolation=postgres_isolation;value=migration_tools(isolation)
    with isolation.temporary_schema() as schema:
        with isolation.admin.begin() as c:
            if mutation=='checksum':c.execute(sa.text('UPDATE '+schema+".wordheat_schema_revisions SET sha256=:sha WHERE revision=:rev"),{'sha':'0'*64,'rev':DRAIN})
            if mutation=='unknown_revision':c.execute(sa.text('INSERT INTO '+schema+".wordheat_schema_revisions(revision,sha256) VALUES('20991231-future.sql',:sha)"),{'sha':'1'*64})
            before=c.execute(sa.text('SELECT revision,sha256 FROM '+schema+'.wordheat_schema_revisions ORDER BY revision')).all()
        with pytest.raises(DomainError) as error:apply(value,isolation,schema,2 if mutation=='future_target' else 1)
        assert error.value.code==('MIGRATION_CHECKSUM_MISMATCH' if mutation=='checksum' else 'MIGRATION_PACKAGE_REQUIRED')
        with isolation.admin.connect() as c:assert c.execute(sa.text('SELECT revision,sha256 FROM '+schema+'.wordheat_schema_revisions ORDER BY revision')).all()==before

def test_real_installed_sql_error_rolls_back_all_earlier_sql_and_revision_rows(postgres_isolation):
    isolation=postgres_isolation;value=migration_tools(isolation)
    with isolation.temporary_schema() as schema:
        reset_runtime(isolation,schema)
        with isolation.admin.begin() as c:
            c.exec_driver_sql('ALTER TABLE '+schema+'.rounds DROP COLUMN drain_until_ns,DROP COLUMN close_seq')
            c.exec_driver_sql('CREATE TABLE '+schema+'.runtime_commands(id text PRIMARY KEY)')
        with pytest.raises(DomainError) as error:apply(value,isolation,schema)
        assert error.value.code=='MIGRATION_EXECUTION_FAILED'
        with isolation.admin.connect() as c:
            assert c.execute(sa.text('SELECT count(*) FROM '+schema+'.wordheat_schema_revisions WHERE revision IN (:r,:d)'),{'r':RUNTIME,'d':DRAIN}).scalar_one()==0
            assert c.execute(sa.text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema AND table_name='rounds' AND column_name='drain_until_ns'"),{'schema':schema}).scalar_one()==0
            assert c.execute(sa.text('SELECT to_regclass(:table)'),{'table':schema+'.runtime_instances'}).scalar_one() is None

def test_registered_native_executor_dispatches_migrate_schema_to_real_noop(postgres_isolation,tmp_path):
    from app.modules.operations.runtime_artifacts import ArtifactRegistry
    from app.modules.operations.runtime_native import NativeMaintenancePort
    from app.infrastructure.runtime_configuration import RegisteredBackupExecutor
    fixture=Path(__file__).parents[2]/'unit/test_runtime_artifacts.py'
    spec=importlib.util.spec_from_file_location('signed_migration_fixture',fixture);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    folder,public,payload,write,path=module.artifacts.__wrapped__(tmp_path)
    payload['installation_id']='wordheat-local-development';payload['compatibility']['db_schema_write_version']=1;write(payload)
    registry=ArtifactRegistry(folder,{'fixture':public},'wordheat-local-development');registry.register('installed-release',path)
    current=module.current()|{'installation_id':'wordheat-local-development'}
    plan=registry.prepare('upgrade_app','installed-release','migration-command',current)
    isolation=postgres_isolation;value=migration_tools(isolation)
    with isolation.temporary_schema() as schema:
        value.registry['source']={'key':'local','schema':schema,'namespace_marker':isolation.owned[schema][1]}
        core=SimpleNamespace(db=SimpleNamespace(test_namespace=None),scorer=None,export_verified_bundle=None)
        port=NativeMaintenancePort(registry,lambda attest:RegisteredBackupExecutor(attest,tools=SimpleNamespace(load=lambda:value),registry=registry,core=core))
        proof=port.run_stage('MIGRATE_SCHEMA',plan,{})
        assert proof['noop'] is True and proof['schema_version']==1
        reconciled=port.reconcile('MIGRATE_SCHEMA',plan,{})
        assert reconciled['completed'] is True and reconciled['result']['noop'] is True
        payload['compatibility']['db_schema_write_version']=2;write(payload);registry.register('future-release',path)
        future=registry.prepare('upgrade_app','future-release','future-command',current)
        with pytest.raises(DomainError) as error:port.run_stage('MIGRATE_SCHEMA',future,{})
        assert error.value.code=='MIGRATION_PACKAGE_REQUIRED'


def test_migration_rejects_wrong_source_marker_without_changing_any_revision(postgres_isolation):
    isolation=postgres_isolation;value=migration_tools(isolation)
    with isolation.temporary_schema() as schema:
        with isolation.admin.connect() as c:before=c.execute(sa.text('SELECT revision,sha256 FROM '+schema+'.wordheat_schema_revisions ORDER BY revision')).all()
        with pytest.raises(DomainError) as error:
            value.migrate_installed('local',schema=schema,namespace_marker='wordheat-live:pytest:'+'0'*64,installation_id='wordheat-local-development',target_schema_version=1)
        assert error.value.code=='POSTGRES_INSTANCE_UNVERIFIED'
        with isolation.admin.connect() as c:assert c.execute(sa.text('SELECT revision,sha256 FROM '+schema+'.wordheat_schema_revisions ORDER BY revision')).all()==before

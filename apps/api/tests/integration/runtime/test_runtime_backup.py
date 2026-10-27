"""Real custom archive and independent registered logical-DB restore, owned schema only."""
import importlib.util
import os
from pathlib import Path
import pytest
import sqlalchemy as sa
from app.errors import DomainError
from app.modules.operations import runtime_postgres as module
spec=importlib.util.spec_from_file_location('runtime_command_fixture',Path(__file__).with_name('test_runtime_commands.py'))
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
env=h.env

def tools():
    assert hasattr(module,'PostgresTools'),'fixed registered PostgreSQL backup tools are missing'
    return module.PostgresTools.from_registry(Path(os.environ['WORDHEAT_MAINTENANCE_REGISTRY_FILE']),'wordheat-local-development')

def test_real_owned_schema_dump_list_restore_and_no_source_or_registry_mutation(env,postgres_isolation,tmp_path):
    e=env;value=tools();schema=postgres_isolation.schema
    with e.db.engine.connect() as c:
        marker=c.execute(sa.text('SELECT obj_description(oid,\'pg_namespace\') FROM pg_namespace WHERE nspname=:schema'),{'schema':schema}).scalar_one()
        count=c.execute(sa.text('SELECT count(*) FROM operators')).scalar_one()
    archive=tmp_path/'owned.dump'
    manifest=value.dump('local',archive,schema=schema,namespace_marker=marker)
    assert archive.read_bytes()[:5]==b'PGDMP' and archive.stat().st_mode&0o077==0
    listing=value.list_archive(archive)
    assert listing['entries']>0 and listing['sha256']==manifest['sha256']
    restored=False
    try:
        proof=value.restore('restore-validation',archive,manifest)
        restored=True
        assert proof['independent_instance_id']=='wordheat-restore-validation' and proof['restore_verified'] is True
        assert proof['source_instance_id']=='wordheat-local-development' and proof['schema']==schema
        with e.db.engine.connect() as c:assert c.execute(sa.text('SELECT count(*) FROM operators')).scalar_one()==count
        with pytest.raises(DomainError) as error:value.restore('local',archive,manifest)
        assert error.value.code=='RESTORE_TARGET_REJECTED'
        with pytest.raises(DomainError) as error:value.restore('restore-validation',archive,manifest)
        assert error.value.code=='RESTORE_TARGET_NOT_EMPTY'
    finally:
        if restored:value.cleanup_owned_restore('restore-validation',schema,manifest['namespace_marker'],manifest['backup_id'])

def test_dump_scope_rejects_public_and_no_shell_or_arbitrary_program(env,tmp_path):
    value=tools()
    for schema in ['wordheat_restore_control','other; DROP DATABASE x']:
        with pytest.raises(DomainError):value.dump('local',tmp_path/'bad.dump',schema=schema,namespace_marker='unknown')
    assert not hasattr(value,'exec') and not hasattr(value,'shell')

def test_confirmed_backup_and_real_independent_verify_update_c1_status(env,postgres_isolation,tmp_path):
    import json
    from app.modules.operations.runtime_artifacts import ArtifactRegistry
    from app.modules.operations.runtime_native import NativeMaintenancePort
    from app.modules.operations.runtime_backup import BackupExecutor
    from app.modules.operations import runtime_models as m
    e=env;tool=tools()
    with e.db.engine.connect() as c:marker=c.execute(sa.text("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=:name"),{'name':postgres_isolation.schema}).scalar_one()
    registry=ArtifactRegistry(tmp_path,{},'wordheat-local-development')
    port=NativeMaintenancePort(registry,lambda attest:BackupExecutor(attest,tools=tool,registry=registry,database=e.db,source_key='local',source_schema=postgres_isolation.schema,namespace_marker=marker))
    value=h.controller(e,native_port=port)
    result=h.request(value,e,'request_maintenance','backup_local');value.tick()
    native=value.native_control_port();review=native.prepare(result['command_id']);native.confirm(result['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id']);value.tick();value.tick()
    with e.db.engine.connect() as c:
        current=value.lookup_command(c,result['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='applied',current.get('error')
        backup=value.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['backup_status']['local']
        assert backup['status']=='completed' and backup['last_completed_at'] and backup['last_verified_at'] is None
        stage=c.execute(sa.select(m.steps.c.evidence).where(m.steps.c.command_id==result['command_id'],m.steps.c.stage=='CREATE_BACKUP')).scalar_one()['result']
    role_plan=json.loads((Path(stage['manifest_path']).parent/'roles-no-password.json').read_text())
    assert role_plan['passwords_included'] is False and all('PASSWORD' not in statement for statement in role_plan['statements'])
    verify=h.request(value,e,'request_maintenance','verify_local_backup',key='verify');value.tick()
    review=native.prepare(verify['command_id'],stage['backup_artifact_id']);native.confirm(verify['command_id'],review['fingerprint'],review['confirmation_nonce'],review['installation_id']);value.tick();value.tick()
    with e.db.engine.connect() as c:
        current=value.lookup_command(c,verify['command_id'],e.actor,['maintenance.request'])
        assert current['status']=='applied',current.get('error')
        backup=value.status(c,{'checks':{},'can_test_live':False,'can_go_live':False},True)['backup_status']['local']
        assert backup['status']=='verified' and backup['last_verified_at']
        proof=c.execute(sa.select(m.steps.c.evidence).where(m.steps.c.command_id==verify['command_id'],m.steps.c.stage=='VERIFY_RESTORE')).scalar_one()['result']
        assert proof['restore_verified'] and proof['security_transforms']['operator_sessions_expired'] and proof['active_configuration_switched'] is False

def test_public_backup_restore_preserves_original_empty_target_namespace_and_source(env,tmp_path):
    """Root-authorized readonly source public backup; dump remains private and is never printed."""
    e=env;value=tools()
    source=value.database('local')
    marker=value._psql(source,"SELECT to_json(obj_description(oid,'pg_namespace')) FROM pg_namespace WHERE nspname='public'")
    target=value.database('restore-validation',target=True)
    original=value._namespace_metadata(target,'public')
    archive=tmp_path/'private-public.dump';restored=False
    import hashlib
    from importlib.resources import files
    installed=files('migrations.versions').joinpath('20261026-connector-control.sql')
    expected=hashlib.sha256(installed.read_text().encode()).hexdigest()
    revision=value._psql(source,"SELECT to_json(sha256) FROM public.wordheat_schema_revisions WHERE revision='20261026-connector-control.sql'")
    if revision!=expected:
        # An un-migrated historical public DB is never silently accepted as the
        # current application schema. Source migration remains the root's action.
        before=value._schema_facts(source,'public')['dataset_sha256']
        with pytest.raises(DomainError) as error:
            value.dump('local',archive,schema='public',namespace_marker=marker)
        assert error.value.code=='BACKUP_SCHEMA_MIGRATION_REQUIRED'
        assert not archive.exists()
        assert value._namespace_metadata(target,'public')==original
        assert value._schema_facts(source,'public')['dataset_sha256']==before
        return
    with e.db.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        with c.begin():
            c.execute(sa.text('SET TRANSACTION READ ONLY'))
            manifest=value.dump('local',archive,schema='public',namespace_marker=marker,connection=c)
    before=value._schema_facts(source,'public')['dataset_sha256']
    try:
        try:proof=value.restore('restore-validation',archive,manifest);restored=True
        except DomainError as error:raise AssertionError({'code':error.code,'details':error.details}) from None
        assert proof['restore_verified'] is True and proof['business_table_counts_match'] is True
        assert proof['security_transforms']['active_configuration_switched'] is False
        with pytest.raises(DomainError):value.restore('restore-validation',archive,manifest)
    finally:
        owned=value._namespace_metadata(target,'public')
        if restored or owned and owned.get('comment')==value.job_marker(manifest['backup_id'],manifest['sha256']):value.cleanup_owned_restore('restore-validation','public',manifest['namespace_marker'],manifest['backup_id'])
    assert value._namespace_metadata(target,'public')==original
    assert value._schema_facts(source,'public')['dataset_sha256']==before
    assert archive.stat().st_mode&0o077==0

def test_exported_backup_snapshot_ignores_concurrent_real_analytics_copy(env,postgres_isolation,tmp_path,monkeypatch):
    from app.modules.analytics import models as analytics
    e=env;value=tools();schema=postgres_isolation.schema
    with e.db.engine.connect() as c:marker=c.execute(sa.text("SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=:schema"),{'schema':schema}).scalar_one()
    original=value._run;created=[]
    def run(program,args,**kwargs):
        if program=='pg_dump' and not created:
            assert any(x.startswith('--snapshot=') for x in args)
            created.append(e.svc.snapshot(e.sid,e.actor)['snapshot_id'])
        return original(program,args,**kwargs)
    monkeypatch.setattr(value,'_run',run)
    with e.db.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
        with c.begin():
            c.execute(sa.text('SET TRANSACTION READ ONLY'))
            metadata=value.dump('local',tmp_path/'concurrent.dump',schema=schema,namespace_marker=marker,connection=c)
    assert metadata['consistency']=='exported_snapshot' and metadata['table_counts']['analytics_snapshots']==0
    with e.db.engine.connect() as c:assert c.execute(sa.select(analytics.snapshots.c.id).where(analytics.snapshots.c.id==created[0])).scalar_one()==created[0]
    proof=None
    try:
        proof=value.restore('restore-validation',tmp_path/'concurrent.dump',metadata)
        assert proof['restore_verified'] is True
    finally:
        if proof:value.cleanup_owned_restore('restore-validation',schema,metadata['namespace_marker'],metadata['backup_id'])

@pytest.mark.parametrize('mutation',['function_body','trigger_event','trigger_target'])
def test_same_named_untrusted_function_or_trigger_is_not_accepted(postgres_isolation,tmp_path,mutation):
    value=tools()
    with postgres_isolation.temporary_schema() as schema:
        with postgres_isolation.admin.begin() as c:
            c.execute(sa.text("SELECT set_config('search_path',:name,true)"),{'name':schema})
            if mutation=='function_body':
                c.exec_driver_sql('CREATE OR REPLACE FUNCTION '+schema+'.wordheat_reject_room_binding_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;')
            else:
                c.exec_driver_sql('DROP TRIGGER immutable_room_binding ON '+schema+'.connector_room_bindings')
                table='connector_room_bindings' if mutation=='trigger_event' else 'guesses'
                event='INSERT' if mutation=='trigger_event' else 'UPDATE'
                c.exec_driver_sql('CREATE TRIGGER immutable_room_binding BEFORE '+event+' ON '+schema+'.'+table+' FOR EACH ROW EXECUTE FUNCTION '+schema+'.wordheat_reject_room_binding_mutation()')
        marker=postgres_isolation.owned[schema][1]
        with pytest.raises(DomainError) as error:value.dump('local',tmp_path/'rejected.dump',schema=schema,namespace_marker=marker)
        assert error.value.code=='BACKUP_TRUSTED_CODE_MISMATCH'
        # Deliberately craft an archive with the registered non-super source tool to
        # prove the independent archive verifier also rejects before any restore SQL.
        source=value.database('local');archive=tmp_path/'untrusted-archive.dump'
        with archive.open('wb') as stream:value._run('pg_dump',['--host='+value.pg_socket,'--username='+source.owner,'--no-password','--dbname='+source.database,'--format=custom','--no-owner','--no-privileges','--schema='+schema],output_file=stream)
        archive.chmod(0o600)
        with pytest.raises(DomainError) as error:value.list_archive(archive,expected_schema=schema)
        assert error.value.code in {'BACKUP_TRUSTED_CODE_MISMATCH','BACKUP_TOC_REJECTED'}


def test_missing_trusted_pair_is_rejected_in_source_and_independent_archive(postgres_isolation,tmp_path):
    value=tools()
    with postgres_isolation.temporary_schema() as schema:
        with postgres_isolation.admin.begin() as c:
            c.exec_driver_sql('DROP TRIGGER immutable_room_binding ON '+schema+'.connector_room_bindings')
            c.exec_driver_sql('DROP FUNCTION '+schema+'.wordheat_reject_room_binding_mutation()')
        marker=postgres_isolation.owned[schema][1]
        with pytest.raises(DomainError) as error:value.dump('local',tmp_path/'missing-source.dump',schema=schema,namespace_marker=marker)
        assert error.value.code=='BACKUP_TRUSTED_CODE_MISMATCH'
        source=value.database('local');archive=tmp_path/'missing-pair.dump'
        with archive.open('wb') as stream:value._run('pg_dump',['--host='+value.pg_socket,'--username='+source.owner,'--no-password','--dbname='+source.database,'--format=custom','--no-owner','--no-privileges','--schema='+schema],output_file=stream)
        archive.chmod(0o600)
        with pytest.raises(DomainError) as error:value.list_archive(archive,expected_schema=schema)
        assert error.value.code=='BACKUP_SCHEMA_MIGRATION_REQUIRED'

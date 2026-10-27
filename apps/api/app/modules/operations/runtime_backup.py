"""Portable fixed backup/verify/restore executor, usable on Mac with registered PG tools."""
from contextlib import contextmanager
from datetime import datetime,timezone
import json
import os
from pathlib import Path
from app.errors import DomainError
from app.modules.operations.runtime_artifacts import sha_file

class BackupExecutor:
    def __init__(self,attest,*,tools,registry,database,source_key,source_schema,namespace_marker,verification_target='restore-validation',platform_executor=None,scorer=None,context_provider=None):
        self.attest=attest;self.tools=tools;self.registry=registry;self.database=database
        self.source_key=source_key;self.source_schema=source_schema;self.namespace_marker=namespace_marker
        self.verification_target=verification_target;self.platform_executor=platform_executor;self.scorer=scorer;self.context_provider=context_provider

    def proof(self,stage,plan,result):return self.attest(stage,plan.payload['command_id'],plan.fingerprint,result)

    @staticmethod
    def write_private(path,value):
        raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())

    def backup_manifest(self,plan,prior):
        created=prior.get('CREATE_BACKUP')
        if created:return created['database_backup'],Path(created['archive']['path'])
        payload=plan.payload.get('artifact') or {}
        metadata=payload.get('database_backup')
        paths=plan.payload['artifact_paths']
        if not metadata or not paths:raise DomainError('BACKUP_EVIDENCE_REQUIRED','Registered backup metadata required',409)
        path=next((Path(p) for p in paths if sha_file(Path(p))==metadata.get('sha256')),None)
        if path is None:raise DomainError('BACKUP_INTEGRITY_FAILED','Backup file does not match registered metadata',409)
        return metadata,path

    def run_stage(self,stage,plan,prior):
        p=plan.payload
        if stage in {'STAGE_MODEL','WARM_MODEL','SELF_CHECK','ACTIVATE_POINTER','INSTALL_VERSION'}:
            from app.modules.operations.runtime_artifacts import verify_model_data
            artifact=p.get('artifact') or {};bundle=artifact.get('bundle') or {}
            if stage=='INSTALL_VERSION':
                # Private staging is portable; protected executable activation is a separate native step.
                destination=self.registry.root/('staged-'+p['command_id']);destination.mkdir(mode=0o700,exist_ok=True)
                for source in p['artifact_paths']:
                    source=Path(source);relative=source.relative_to(self.registry.root);target=destination/relative;target.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
                    if target.exists():
                        if sha_file(target)!=sha_file(source):raise DomainError('MAINTENANCE_ARTIFACT_CHANGED','Staged release bytes changed',409)
                    else:
                        with source.open('rb') as incoming:
                            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                            with os.fdopen(fd,'wb') as outgoing:__import__('shutil').copyfileobj(incoming,outgoing,1024*1024)
                return self.proof(stage,plan,{'private_release_staged':True,'protected_executables_activated':False})
            manifest_name=artifact.get('model_manifest');root=self.registry.model_roots.get(artifact.get('model_root_id'))
            if not manifest_name or not root:raise DomainError('MODEL_RESTART_PLAN_INVALID','Registered model manifest/root required',409)
            manifest_path=self.registry.path(manifest_name)
            if str(manifest_path) not in p['artifact_paths']:raise DomainError('MODEL_RESTART_PLAN_INVALID','Manifest is not signed into the plan',409)
            verified=verify_model_data(manifest_path,root)
            if verified['manifest_sha256']!=bundle.get('model_manifest_sha256') or verified['manifest']['manifest_id']!=bundle.get('model_manifest_id'):raise DomainError('CANDIDATE_MISMATCH','Model bytes differ from confirmed bundle',409)
            if stage=='STAGE_MODEL':return self.proof(stage,plan,{'model_data_verified':True,'model_manifest_sha256':verified['manifest_sha256']})
            try:observed=self.scorer.readiness() if self.scorer is not None else {}
            except Exception:observed={}
            declared=getattr(self.scorer,'manifest_id',None)==bundle['model_manifest_id'] and getattr(self.scorer,'manifest_sha256',None)==bundle['model_manifest_sha256']
            if declared and observed.get('ready') is not True:raise DomainError('MODEL_PROBE_PENDING','Current scorer identity is configured but a fresh verified probe is pending',503,True)
            matches=observed.get('ready') is True and observed.get('manifest_id')==bundle['model_manifest_id'] and getattr(self.scorer,'manifest_sha256',None)==bundle['model_manifest_sha256']
            if not matches:raise DomainError('MODEL_RESTART_REQUIRED','Actual registered scorer must restart and warm this bundle',409,details={'stage':stage,'declared_id':getattr(self.scorer,'manifest_id',None),'declared_sha':getattr(self.scorer,'manifest_sha256',None),'observed_id':observed.get('manifest_id'),'observed_ready':observed.get('ready'),'expected_id':bundle['model_manifest_id'],'expected_sha':bundle['model_manifest_sha256']})
            if stage in {'WARM_MODEL','SELF_CHECK'}:return self.proof(stage,plan,{'actual_scorer_ready':True,'model_manifest_id':bundle['model_manifest_id'],'model_manifest_sha256':bundle['model_manifest_sha256']})
            pointer=self.registry.root/'.active-bundle.json'
            if pointer.is_symlink():raise DomainError('MAINTENANCE_PATH_REJECTED','Active pointer path rejected',409)
            temporary=self.registry.root/('.active-'+p['command_id']+'.part')
            if temporary.exists():
                if temporary.is_symlink():raise DomainError('MAINTENANCE_PATH_REJECTED','Active pointer path rejected',409)
                temporary.unlink()
            self.write_private(temporary,{'version':1,'installation_id':p['installation_id'],'command_id':p['command_id'],'plan_fingerprint':plan.fingerprint,'manifest_path':str(manifest_path),'model_root':root,'bundle':bundle})
            os.replace(temporary,pointer)
            return self.proof(stage,plan,{'data_bundle_pointer_updated':True,'protected_executables_activated':False})
        if stage=='MIGRATE_SCHEMA':
            self.registry.recheck(plan)
            artifact=p.get('artifact') or {};compatibility=artifact.get('compatibility') or {}
            before=p.get('before') or {}
            if p.get('operation')!='upgrade_app' or compatibility.get('pg_major')!=16 or before.get('schema_version') not in compatibility.get('upgrade_from_schema_versions',[]) or before.get('schema_version') not in compatibility.get('db_schema_read_versions',[]):raise DomainError('MAINTENANCE_INCOMPATIBLE','Confirmed upgrade schema compatibility required',409)
            result=self.tools.migrate_installed(self.source_key,schema=self.source_schema,namespace_marker=self.namespace_marker,installation_id=p['installation_id'],target_schema_version=compatibility.get('db_schema_write_version'))
            return self.proof(stage,plan,result)
        if stage=='CREATE_BACKUP':
            folder=self.registry.root/('backup-'+p['command_id'])
            folder.mkdir(mode=0o700,exist_ok=False)
            archive=folder/'database.dump'
            with self.database.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
                with c.begin():
                    c.execute(__import__('sqlalchemy').text('SET TRANSACTION READ ONLY'))
                    metadata=self.tools.dump(self.source_key,archive,schema=self.source_schema,namespace_marker=self.namespace_marker,connection=c)
            before=p['before'];stamp=metadata['created_at'];rpo=metadata.get('snapshot_captured_at',stamp)
            backup=dict(backup_id=metadata['backup_id'],status='running',last_completed_at=stamp,last_verified_at=None,app_version=before['app_version'],schema_version=before['schema_version'],model_manifest_id=before.get('model_manifest_id'),location_hint='private-backup:'+metadata['backup_id'],last_error=None)
            roles=[]
            for role in metadata['roles']:
                if role['rolsuper'] or role['rolcreatedb'] or role['rolcreaterole'] or role['rolreplication'] or role['rolbypassrls']:raise DomainError('BACKUP_ROLE_PLAN_UNSAFE','Backup roles are not least privilege',409)
                name='"'+role['rolname'].replace('"','""')+'"'
                roles.append('CREATE ROLE '+name+(' LOGIN' if role['rolcanlogin'] else ' NOLOGIN')+' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'+(' INHERIT' if role['rolinherit'] else ' NOINHERIT')+';')
            files=[dict(path=str(archive.relative_to(self.registry.root)),bytes=archive.stat().st_size,sha256=metadata['sha256'])]
            role_path=folder/'roles-no-password.json';self.write_private(role_path,{'version':1,'statements':roles,'passwords_included':False,'requires_native_credential_rebind':True})
            context_path=folder/'context.json';self.write_private(context_path,dict(version=1,source_instance_id=before['installation_id'],app_version=before['app_version'],schema_version=before['schema_version'],model_manifest_id=before.get('model_manifest_id'),model_manifest_sha256=before.get('model_manifest_sha256'),contract_version='LW-C1/1.0.0',schema_revisions=metadata['revisions'],configuration={'installation_id':before['installation_id']},captured_at=rpo,rpo_boundary=rpo,source_database_unchanged=True))
            for path in [role_path,context_path]:files.append(dict(path=str(path.relative_to(self.registry.root)),bytes=path.stat().st_size,sha256=sha_file(path)))
            payload=dict(format='wordheat-local-backup/1',installation_id=before['installation_id'],operation='backup_local',files=files,backup=backup,database_backup=metadata,required_free_bytes=2*archive.stat().st_size)
            manifest=folder/'backup.json';self.write_private(manifest,payload)
            artifact_id='backup:'+metadata['backup_id'];self.registry.register_backup(artifact_id,manifest,sha_file(manifest))
            return self.proof(stage,plan,dict(archive={'path':str(archive),'bytes':archive.stat().st_size,'sha256':metadata['sha256']},backup=backup,database_backup=metadata,backup_artifact_id=artifact_id,manifest_sha256=sha_file(manifest),manifest_path=str(manifest),rpo_boundary=rpo))
        if stage=='VALIDATE_BACKUP':
            metadata,path=self.backup_manifest(plan,prior)
            if sha_file(path)!=metadata['sha256'] or path.stat().st_size!=metadata['bytes']:raise DomainError('BACKUP_INTEGRITY_FAILED','Backup bytes changed',409)
            listed=self.tools.list_archive(path,expected_schema=metadata['schema'])
            return self.proof(stage,plan,dict(archive_validated=True,entries=listed['entries'],archive_sha256=listed['sha256']))
        if stage in {'CREATE_RESTORE_TARGET','VERIFY_RESTORE'}:
            metadata,path=self.backup_manifest(plan,prior)
            target=p['target_id'] or self.verification_target
            if stage=='CREATE_RESTORE_TARGET':
                registered=self.tools.database(target,target=True)
                return self.proof(stage,plan,{'registered_instance_id':registered.instance_id,'new_database_created':False,'source_configuration_changed':False})
            restored=False
            try:
                result=self.tools.restore(target,path,metadata);restored=True
                result.update(rpo_boundary=metadata.get('snapshot_captured_at',metadata['created_at']),device_rebind_required=True,active_configuration_switched=False)
                return self.proof(stage,plan,result)
            finally:
                # Verification uses a disposable owned schema in the registered logical target.
                # A requested restore_new_instance remains available for explicit later activation.
                if restored and p['operation']!='restore_new_instance':self.tools.cleanup_owned_restore(target,metadata['schema'],metadata['namespace_marker'],metadata['backup_id'])
        if stage=='REGISTER_RESTORED_INSTANCE':
            verified=prior.get('VERIFY_RESTORE',{})
            if verified.get('restore_verified') is not True:raise DomainError('BACKUP_RESTORE_UNVERIFIED','Restore must be independently verified first',409)
            return self.proof(stage,plan,dict(instance_id=verified['independent_instance_id'],schema=verified['schema'],requires_native_rebind=True,active_configuration_switched=False,source_unchanged=True))
        if self.platform_executor is not None:return self.platform_executor.run_stage(stage,plan,prior)
        raise DomainError('WINDOWS_NATIVE_REQUIRED','Protected installation/process activation requires its fixed native executor',503)

    def reconcile(self,stage,plan,prior):
        p=plan.payload
        if stage in {'STAGE_MODEL','WARM_MODEL','SELF_CHECK','ACTIVATE_POINTER','INSTALL_VERSION'}:
            result=self.run_stage(stage,plan,prior)
            return self.proof(stage,plan,{'completed':True,'result':result.result})
        if stage=='CREATE_BACKUP':
            folder=self.registry.root/('backup-'+p['command_id']);manifest=folder/'backup.json'
            if not folder.exists():return self.proof(stage,plan,{'completed':False,'safe_to_retry':True})
            if not manifest.is_file():raise DomainError('BACKUP_PARTIAL_REQUIRES_REPAIR','Partial backup is retained for explicit repair',409)
            artifact_id='recovered-backup:'+p['command_id'];self.registry.register_backup(artifact_id,manifest,sha_file(manifest))
            payload=json.loads(manifest.read_bytes());metadata=payload['database_backup']
            archive=self.registry.path(payload['files'][0]['path']);self.tools.list_archive(archive,expected_schema=metadata['schema'])
            result=dict(archive={'path':str(archive),'bytes':archive.stat().st_size,'sha256':metadata['sha256']},backup=payload['backup'],database_backup=metadata,backup_artifact_id=artifact_id,manifest_path=str(manifest),manifest_sha256=sha_file(manifest),rpo_boundary=metadata.get('snapshot_captured_at',metadata['created_at']))
            return self.proof(stage,plan,{'completed':True,'result':result})
        if stage in {'VALIDATE_BACKUP','CREATE_RESTORE_TARGET','MIGRATE_SCHEMA'}:
            result=self.run_stage(stage,plan,prior)
            return self.proof(stage,plan,{'completed':True,'result':result.result})
        if stage=='VERIFY_RESTORE':
            metadata,path=self.backup_manifest(plan,prior);target=p['target_id'] or self.verification_target
            if p['operation']=='restore_new_instance':
                result=self.tools.verify_owned_restore(target,path,metadata)
                return self.proof(stage,plan,{'completed':True,'result':result})
            # Verification targets are cleaned only with marker/OID/job proof. A crashed
            # post-restore window is revalidated, not overwritten or guessed completed.
            try:
                result=self.tools.verify_owned_restore(target,path,metadata)
            except DomainError as error:
                entry=self.tools.database(target,target=True)
                job=self.tools._psql(entry,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+__import__('app.modules.operations.runtime_postgres',fromlist=['literal']).literal(metadata['backup_id']))
                if job and job['state'] not in {'cleaned'}:raise
                return self.proof(stage,plan,{'completed':False,'safe_to_retry':True})
            self.tools.cleanup_owned_restore(target,metadata['schema'],metadata['namespace_marker'],metadata['backup_id'])
            return self.proof(stage,plan,{'completed':True,'result':result})
        if stage=='REGISTER_RESTORED_INSTANCE':return self.proof(stage,plan,{'completed':False,'safe_to_retry':True})
        if self.platform_executor is not None and callable(getattr(self.platform_executor,'reconcile',None)):return self.platform_executor.reconcile(stage,plan,prior)
        raise DomainError('MAINTENANCE_RECOVERY_REQUIRED','Fixed native stage needs explicit reconciliation',409)

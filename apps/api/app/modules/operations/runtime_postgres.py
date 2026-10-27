"""Registered local PG16 backup tools. Docker is a fixed development adapter only.

No shell, SSH, lifecycle, database/role creation, arbitrary argv, passwords,
source cleanup or unregistered database is exposed by this interface.
"""
from dataclasses import dataclass,replace
from datetime import datetime,timezone
import hashlib
import stat
from importlib.resources import files
from urllib.parse import urlsplit
import json
import os
from pathlib import Path
import re
import secrets
import select
import time
import shutil
import subprocess
from app.errors import DomainError
from app.modules.operations.runtime_artifacts import sha_file

IDENTIFIER=re.compile(r'^[a-z_][a-z0-9_]{0,62}$')
SCHEMA=re.compile(r'^wordheat_pytest_[0-9a-f]{32}$')

def fail(code):raise DomainError(code,'Registered PostgreSQL backup operation failed',409)
def literal(value):return "'"+str(value).replace("'","''")+"'"
def ident(value):
    if not isinstance(value,str) or not IDENTIFIER.fullmatch(value):fail('POSTGRES_SCOPE_REJECTED')
    return '"'+value+'"'

@dataclass(frozen=True)
class RegisteredDatabase:
    key:str
    database:str
    marker:str
    instance_id:str
    owner:str
    restore_target:bool=False
    source_database:str|None=None
    allow_full_backup:bool=False
    runtime_role:str|None=None

class PostgresTools:
    def __init__(self,registry):
        if not isinstance(registry,dict) or registry.get('version')!=1:fail('POSTGRES_REGISTRY_INVALID')
        transport=registry.get('transport',{})
        if transport.get('kind')!='docker-unix':raise DomainError('POSTGRES_TOOL_CONFIGURATION_REQUIRED','Registered local PostgreSQL tool adapter required',503)
        endpoint=transport.get('endpoint','');parts=urlsplit(endpoint)
        if parts.scheme!='unix' or parts.netloc or not Path(parts.path).is_absolute():fail('POSTGRES_ENDPOINT_REJECTED')
        socket_path=Path(parts.path)
        if not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):fail('POSTGRES_ENDPOINT_REJECTED')
        container=transport.get('container',{});program=transport.get('program',{});storage=container.get('storage',{})
        if not re.fullmatch('[0-9a-f]{64}',container.get('id','')) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',container.get('name','')):fail('POSTGRES_INSTANCE_UNVERIFIED')
        executable=Path(program.get('path',''))
        if not executable.is_absolute() or not executable.is_file() or executable.is_symlink() or executable.stat().st_mode&0o022 or not re.fullmatch('[0-9a-f]{64}',program.get('sha256','')) or sha_file(executable)!=program['sha256']:fail('POSTGRES_TOOL_IDENTITY_CHANGED')
        if storage.get('type')!='bind' or storage.get('rw') is not True or not Path(storage.get('source','')).is_absolute() or not Path(storage.get('target','')).is_absolute():fail('POSTGRES_INSTANCE_UNVERIFIED')
        self.databases={item['key']:RegisteredDatabase(**item) for item in registry.get('databases',[])}
        if not self.databases or len(self.databases)>16:fail('POSTGRES_REGISTRY_INVALID')
        self.endpoint=endpoint;self.container_id=container['id'];self.container_name=container['name'];self.storage=storage
        self.image_prefix=container.get('image_prefix','');self.pg_socket=transport.get('pg_socket_directory','')
        if not self.image_prefix or not self.pg_socket.startswith('/') or '\x00' in self.pg_socket:fail('POSTGRES_REGISTRY_INVALID')
        self.docker=str(executable);self.docker_sha256=program['sha256'];self.registry=registry
        for entry in self.databases.values():
            ident(entry.database);ident(entry.owner)
            if entry.runtime_role:ident(entry.runtime_role)
            if not entry.database.startswith('wordheat_') or 'prod' in entry.database:fail('POSTGRES_SCOPE_REJECTED')

    @classmethod
    def from_registry(cls,path,expected_installation_id):
        if path is None:raise DomainError('POSTGRES_TOOL_CONFIGURATION_REQUIRED','Private registered maintenance tool file is missing',503)
        path=Path(path)
        if not path.is_absolute() or any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid() or path.stat().st_size>256*1024:fail('POSTGRES_REGISTRY_INVALID')
        from app.modules.operations.runtime_artifacts import unique
        try:value=json.loads(path.read_bytes(),object_pairs_hook=unique)
        except (ValueError,TypeError):fail('POSTGRES_REGISTRY_INVALID')
        if value.get('installation_id')!=expected_installation_id:fail('POSTGRES_INSTANCE_UNVERIFIED')
        return cls(value)

    def program_identity(self):
        path=Path(self.docker)
        if path.is_symlink() or path.stat().st_mode&0o022 or sha_file(path)!=self.docker_sha256:fail('POSTGRES_TOOL_IDENTITY_CHANGED')
        return {'path':self.docker,'sha256':self.docker_sha256}

    def inspect(self):
        self.program_identity()
        fmt='{{json .Id}}\n{{json .Name}}\n{{json .State.Running}}\n{{json .Mounts}}\n{{json .Config.Image}}'
        result=subprocess.run([self.docker,'--host',self.endpoint,'inspect','--format',fmt,self.container_id],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10,check=False)
        try:
            values=[json.loads(line) for line in result.stdout.splitlines()]
            if result.returncode or len(values)!=5 or values[0]!=self.container_id or values[1]!='/'+self.container_name or values[2] is not True:raise ValueError()
            mounts=[m for m in values[3] if m.get('Destination')==self.storage['target']]
            if len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('Source')!=self.storage['source'] or mounts[0].get('RW') is not True:raise ValueError()
            if not isinstance(values[4],str) or not values[4].startswith(self.image_prefix):raise ValueError()
        except (ValueError,TypeError,KeyError):fail('POSTGRES_INSTANCE_UNVERIFIED')

    def _run(self,program,args,*,input_file=None,output_file=None,timeout=60):
        if program not in {'psql','pg_dump','pg_restore'}:fail('POSTGRES_TOOL_REJECTED')
        self.program_identity()
        command=[self.docker,'--host',self.endpoint,'exec','--user','postgres','--interactive',self.container_id,program,*args]
        # All args are built below from registered data and checked identifiers, never request argv.
        try:
            value=subprocess.run(command,stdin=input_file or subprocess.DEVNULL,stdout=output_file or subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False)
        except (OSError,subprocess.TimeoutExpired):raise DomainError('POSTGRES_TOOL_FAILED','Fixed PostgreSQL tool failed or timed out',503) from None
        if value.returncode:
            state=re.search(rb'ERROR:\s*([0-9A-Z]{5})',value.stderr or b'') if program=='psql' else None
            raise DomainError('POSTGRES_TOOL_FAILED','Fixed PostgreSQL tool rejected the operation',503,details={'sqlstate':state.group(1).decode() if state else None}) from None
        raw=b'' if output_file else value.stdout
        if len(raw)>4*1024*1024:fail('POSTGRES_TOOL_OUTPUT_LIMIT')
        return raw

    def _psql(self,entry,sql,*,snapshot=None):
        if snapshot:
            if not re.fullmatch(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+',snapshot):fail('POSTGRES_SNAPSHOT_INVALID')
            sql='BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '+literal(snapshot)+'; '+sql+'; COMMIT;'
        sql="SET TIME ZONE 'UTC'; "+sql
        raw=self._run('psql',['--host='+self.pg_socket,'--username='+entry.owner,'--no-password','--dbname='+entry.database,'--no-psqlrc','--quiet','--tuples-only','--no-align','--set=ON_ERROR_STOP=1','--set=VERBOSITY=sqlstate','--command='+sql])
        if not raw.strip():return None
        try:return json.loads(raw)
        except (ValueError,TypeError):fail('POSTGRES_OBSERVATION_INVALID')

    def database(self,key,*,target=False):
        entry=self.databases.get(key)
        if entry is None or target and not entry.restore_target:fail('RESTORE_TARGET_REJECTED' if target else 'POSTGRES_SCOPE_REJECTED')
        self.inspect()
        value=self._psql(entry,"SELECT json_build_object('database',datname,'owner',pg_get_userbyid(datdba),'marker',shobj_description(oid,'pg_database'),'version',current_setting('server_version_num')::int) FROM pg_database WHERE datname=current_database()")
        if not isinstance(value,dict) or type(value.get('version')) is not int:fail('POSTGRES_INSTANCE_UNVERIFIED')
        if value!={'database':entry.database,'owner':entry.owner,'marker':entry.marker,'version':value.get('version')} or value['version']//10000!=16:fail('POSTGRES_INSTANCE_UNVERIFIED')
        role=self._psql(entry,"SELECT json_build_object('superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,'replication',rolreplication,'bypassrls',rolbypassrls) FROM pg_roles WHERE rolname=current_user")
        if not isinstance(role,dict) or any(role.values()):fail('POSTGRES_ROLE_UNSAFE')
        if target:
            registry=self._psql(entry,"SELECT row_to_json(r) FROM wordheat_restore_control.instance_registry r WHERE id='registered'")
            if registry.get('instance_id')!=entry.instance_id or registry.get('marker')!=entry.marker or registry.get('source_database')!=entry.source_database or registry.get('active') is not False:fail('RESTORE_TARGET_REJECTED')
        else:
            instance=self._psql(entry,"SELECT to_json(instance_id) FROM public.service_instance WHERE id='local'")
            if instance!=entry.instance_id:fail('POSTGRES_INSTANCE_UNVERIFIED')
        return entry

    def migrate_installed(self,key,*,schema,namespace_marker,installation_id,target_schema_version):
        # This installed app implements schema 1 only. A signed plan cannot supply
        # SQL or make the current executable claim it can migrate a future schema.
        if type(target_schema_version) is not int or target_schema_version!=1:fail('MIGRATION_PACKAGE_REQUIRED')
        entry=self.database(key)
        if entry.restore_target or entry.instance_id!=installation_id:fail('POSTGRES_SCOPE_REJECTED')
        public=schema=='public' and entry.allow_full_backup
        if not public and (not isinstance(schema,str) or not SCHEMA.fullmatch(schema) or not isinstance(namespace_marker,str) or not namespace_marker.startswith('wordheat-live:pytest:')):fail('POSTGRES_SCOPE_REJECTED')
        ident(schema)
        role=entry.runtime_role
        if not role or role==entry.owner:raise DomainError('POSTGRES_TOOL_CONFIGURATION_REQUIRED','Separate registered migration and runtime roles required',503)
        ident(role)
        resources=sorted((r for r in files('migrations.versions').iterdir() if r.name.endswith('.sql')),key=lambda r:r.name)
        installed=[]
        for resource in resources:
            raw=resource.read_bytes()
            if len(raw)>1024*1024 or not re.fullmatch(r'[0-9]{8}-[a-z0-9-]+\.sql',resource.name):fail('MIGRATION_PACKAGE_REQUIRED')
            try:sql=raw.decode('utf-8')
            except UnicodeError:fail('MIGRATION_PACKAGE_REQUIRED')
            installed.append((resource.name,hashlib.sha256(raw).hexdigest(),sql))
        if not installed:fail('MIGRATION_PACKAGE_REQUIRED')
        expected={name:digest for name,digest,_ in installed}
        package_sha=hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        q=ident(schema)+'.';expected_owner='pg_database_owner' if public else entry.owner
        guard="""DECLARE expected jsonb := EXPECTED; applied jsonb := '[]'::jsonb; actual_version integer;
BEGIN
 IF current_user <> OWNER OR NOT EXISTS(SELECT 1 FROM pg_database WHERE datname=current_database() AND datname=DB AND pg_get_userbyid(datdba)=OWNER AND shobj_description(oid,'pg_database')=MARKER AND current_setting('server_version_num')::int/10000=16) THEN RAISE SQLSTATE 'WH003'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=NS AND pg_get_userbyid(nspowner)=NSOWNER AND obj_description(oid,'pg_namespace') IS NOT DISTINCT FROM NSMARKER) THEN RAISE SQLSTATE 'WH003'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=RUNTIME AND NOT(rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) OR has_schema_privilege(RUNTIME,NS,'CREATE') THEN RAISE SQLSTATE 'WH004'; END IF;
 SELECT schema_version INTO actual_version FROM Qservice_instance WHERE id='local' AND instance_id=INSTANCE;
 IF actual_version IS NULL THEN RAISE SQLSTATE 'WH003'; END IF;
 IF actual_version<>1 THEN RAISE SQLSTATE 'WH001'; END IF;
 IF EXISTS(SELECT 1 FROM Qwordheat_schema_revisions WHERE NOT expected ? revision) THEN RAISE SQLSTATE 'WH001'; END IF;
 IF EXISTS(SELECT 1 FROM Qwordheat_schema_revisions WHERE expected->>revision IS DISTINCT FROM sha256) THEN RAISE SQLSTATE 'WH002'; END IF;
"""
        # Tokens in this fixed template expand only to quoted registry values and
        # checked identifiers. Package SQL is inserted afterwards as a SQL literal.
        values={'EXPECTED':literal(json.dumps(expected))+'::jsonb','NSOWNER':literal(expected_owner),'NSMARKER':('NULL' if namespace_marker is None else literal(namespace_marker)),'RUNTIME':literal(role),'INSTANCE':literal(installation_id),'OWNER':literal(entry.owner),'MARKER':literal(entry.marker),'DB':literal(entry.database),'NS':literal(schema),'Q':q}
        guard=re.sub(r'\b(EXPECTED|NSOWNER|NSMARKER|RUNTIME|INSTANCE|OWNER|MARKER|DB|NS)\b|\bQ(?=service_instance|wordheat_schema_revisions)',lambda m:values[m.group(0)],guard)
        for name,digest,sql in installed:
            guard+=' IF NOT EXISTS(SELECT 1 FROM '+q+'wordheat_schema_revisions WHERE revision='+literal(name)+') THEN\n EXECUTE '+literal(sql)+';\n INSERT INTO '+q+'wordheat_schema_revisions(revision,sha256) VALUES('+literal(name)+','+literal(digest)+');\n applied:=applied || jsonb_build_array('+literal(name)+'); END IF;\n'
        guard+=' EXECUTE '+literal('GRANT USAGE ON SCHEMA '+ident(schema)+' TO '+ident(role))+';\n'
        guard+=' EXECUTE '+literal('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA '+ident(schema)+' TO '+ident(role))+';\n'
        guard+=' EXECUTE '+literal('REVOKE INSERT, UPDATE, DELETE ON '+q+'wordheat_schema_revisions,'+q+'service_instance FROM '+ident(role))+';\n'
        guard+=' IF (SELECT count(*) FROM '+q+"wordheat_schema_revisions)<>(SELECT count(*) FROM jsonb_object_keys(expected)) OR EXISTS(SELECT 1 FROM "+q+"wordheat_schema_revisions WHERE expected->>revision IS DISTINCT FROM sha256) THEN RAISE SQLSTATE 'WH002'; END IF;\n"
        guard+=" PERFORM set_config('wordheat.migration_result',jsonb_build_object('schema_version',1,'applied_revisions',applied,'noop',jsonb_array_length(applied)=0,'package_sha256',"+literal(package_sha)+",'installed_revision_count',(SELECT count(*) FROM jsonb_object_keys(expected)))::text,true);\nEND"
        sql="BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='45s'; SELECT pg_advisory_xact_lock(674219384); SET LOCAL search_path="+ident(schema)+'; DO '+literal(guard)+"; SELECT current_setting('wordheat.migration_result')::json; COMMIT;"
        try:return self._psql(entry,sql)
        except DomainError as error:
            state=(error.details or {}).get('sqlstate')
            code={'WH001':'MIGRATION_PACKAGE_REQUIRED','WH002':'MIGRATION_CHECKSUM_MISMATCH','WH003':'POSTGRES_INSTANCE_UNVERIFIED','WH004':'MIGRATION_RUNTIME_ROLE_UNSAFE'}.get(state,'MIGRATION_EXECUTION_FAILED')
            raise DomainError(code,'Registered installed-schema migration rejected or rolled back',409 if state in {'WH001','WH002','WH003','WH004'} else 503) from None

    def _schema_facts(self,entry,schema,*,snapshot=None):
        namespace=self._psql(entry,"SELECT json_build_object('marker',obj_description(oid,'pg_namespace'),'owner',pg_get_userbyid(nspowner)) FROM pg_namespace WHERE nspname="+literal(schema),snapshot=snapshot)
        tables=self._psql(entry,"SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]'::json) FROM pg_tables WHERE schemaname="+literal(schema),snapshot=snapshot)
        if len(tables)>2000:fail('BACKUP_SCHEMA_TOO_LARGE')
        for table in tables:ident(table)
        union=' UNION ALL '.join('SELECT '+literal(t)+' AS name,count(*) AS n FROM '+ident(schema)+'.'+ident(t) for t in tables)
        counts=self._psql(entry,'SELECT coalesce(json_object_agg(name,n),\'{}\'::json) FROM ('+union+') counted',snapshot=snapshot) if tables else {}
        revisions=self._psql(entry,'SELECT coalesce(json_agg(r ORDER BY revision),\'[]\'::json) FROM '+ident(schema)+'.wordheat_schema_revisions r',snapshot=snapshot)
        roles=self._psql(entry,"SELECT coalesce(json_agg(r),'[]'::json) FROM (SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolinherit,rolreplication,rolbypassrls FROM pg_roles WHERE rolname !~ '^pg_' AND rolname<>'postgres' AND rolname IN (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname="+literal(schema)+" UNION SELECT DISTINCT grantee FROM information_schema.role_table_grants WHERE table_schema="+literal(schema)+" AND grantee<>'PUBLIC')) r",snapshot=snapshot)
        return dict(namespace=namespace,table_counts=counts,revisions=revisions,roles=roles,dataset_sha256=self._dataset_hash(entry,schema,tables,snapshot=snapshot))

    def dump(self,key,output,*,schema,namespace_marker,connection=None):
        entry=self.database(key)
        public=schema=='public' and entry.allow_full_backup
        if entry.restore_target or not public and (not SCHEMA.fullmatch(schema) or not isinstance(namespace_marker,str) or not namespace_marker.startswith('wordheat-live:pytest:')):fail('POSTGRES_SCOPE_REJECTED')
        snapshot=None;captured=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        if connection is not None:
            if connection.engine.url.database!=entry.database or connection.get_isolation_level()!='REPEATABLE READ':fail('POSTGRES_SNAPSHOT_INVALID')
            snapshot=connection.execute(__import__('sqlalchemy').text('SELECT pg_export_snapshot()')).scalar_one()
            captured=connection.execute(__import__('sqlalchemy').text('SELECT transaction_timestamp()')).scalar_one().isoformat(timespec='milliseconds').replace('+00:00','Z')
        self._verify_source_schema_code(entry,schema,snapshot=snapshot)
        facts=self._schema_facts(entry,schema,snapshot=snapshot)
        expected_owner='pg_database_owner' if public else entry.owner
        if facts['namespace']!={'marker':namespace_marker,'owner':expected_owner}:fail('POSTGRES_SCOPE_REJECTED')
        output=Path(output)
        if not output.is_absolute() or output.exists() or output.is_symlink() or output.parent.is_symlink() or output.parent.stat().st_mode&0o077:fail('BACKUP_PATH_REJECTED')
        partial=output.with_name(output.name+'.part-'+secrets.token_hex(8))
        fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                args=['--host='+self.pg_socket,'--username='+entry.owner,'--no-password','--dbname='+entry.database,'--format=custom','--no-owner','--no-privileges','--schema='+schema]
                if snapshot:args.append('--snapshot='+snapshot)
                self._run('pg_dump',args,output_file=stream);stream.flush();os.fsync(stream.fileno())
            os.link(partial,output);partial.unlink()
        except BaseException:
            partial.unlink(missing_ok=True);raise
        metadata=dict(version=1,backup_id=secrets.token_urlsafe(24),source_database=entry.database,source_instance_id=entry.instance_id,schema=schema,namespace_marker=namespace_marker or '',namespace_owner=expected_owner,source_public=public,sha256=sha_file(output),bytes=output.stat().st_size,created_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),snapshot_captured_at=captured,consistency='exported_snapshot' if snapshot else 'quiescent_scope',**facts)
        try:self.list_archive(output,expected_schema=schema)
        except BaseException:
            output.unlink(missing_ok=True);raise
        return metadata

    def list_archive(self,path,*,expected_schema=None):
        path=Path(path)
        if not path.is_file() or path.is_symlink():fail('BACKUP_PATH_REJECTED')
        self.inspect()
        with path.open('rb') as stream:raw=self._run('pg_restore',['--list'],input_file=stream)
        lines=[line for line in raw.decode('utf-8').splitlines() if line and not line.startswith(';')]
        if expected_schema:
            types=['SEQUENCE OWNED BY','FK CONSTRAINT','TABLE DATA','SEQUENCE SET','SCHEMA','TABLE','INDEX','CONSTRAINT','DEFAULT','SEQUENCE','COMMENT','FUNCTION','TRIGGER','ENCODING','STDSTRINGS','SEARCHPATH']
            for line in lines:
                found=re.match(r'^\d+; \d+ \d+ (.+)$',line)
                if not found:fail('BACKUP_TOC_REJECTED')
                rest=found.group(1);kind=next((kind for kind in types if rest.startswith(kind+' ')),None)
                if kind is None:fail('BACKUP_TOC_REJECTED')
                tail=rest[len(kind)+1:].split()
                if kind in {'ENCODING','STDSTRINGS','SEARCHPATH'}:continue
                if kind=='SCHEMA' and len(tail)>1 and tail[:2]==['-',expected_schema]:continue
                if kind=='COMMENT' and len(tail)>2 and tail[:3]==['-','SCHEMA',expected_schema]:continue
                if not tail or tail[0]!=expected_schema:fail('BACKUP_TOC_REJECTED')
                if kind=='FUNCTION' and (len(tail)<2 or tail[1]!='wordheat_reject_room_binding_mutation()'):fail('BACKUP_TOC_REJECTED')
                if kind=='TRIGGER' and (len(tail)<3 or tail[1:3]!=['connector_room_bindings','immutable_room_binding']):fail('BACKUP_TOC_REJECTED')
        if expected_schema:self._verify_archive_schema_code(path,expected_schema,lines)
        return {'entries':len(lines),'sha256':sha_file(path)}

    def _restore_jobs(self,entry):
        relation='wordheat_restore_control.restore_jobs'
        found=self._psql(entry,"SELECT json_build_object('marker',obj_description(oid,'pg_class'),'owner',pg_get_userbyid(relowner)) FROM pg_class WHERE oid=to_regclass('"+relation+"')")
        if found is not None and (found.get('owner')!=entry.owner or found.get('marker') not in {'wordheat-live:restore-jobs:v1','wordheat-live:restore-jobs:v2'}):fail('RESTORE_REGISTRY_UNVERIFIED')
        if found is None:
            self._psql(entry,"CREATE TABLE "+relation+"(backup_id text PRIMARY KEY,schema_name text NOT NULL,namespace_marker text NOT NULL,source_database text NOT NULL,source_instance_id text NOT NULL,archive_sha256 text NOT NULL,state text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()); CREATE UNIQUE INDEX restore_jobs_active_schema ON "+relation+"(schema_name) WHERE state IN ('restoring','verified'); COMMENT ON TABLE "+relation+" IS 'wordheat-live:restore-jobs:v2'; SELECT to_json(true)")
        elif found['marker']=='wordheat-live:restore-jobs:v1':
            constraints=self._psql(entry,"SELECT coalesce(json_agg(conname),'[]'::json) FROM pg_constraint WHERE conrelid='"+relation+"'::regclass AND contype='u' AND conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='"+relation+"'::regclass AND attname='schema_name')]::smallint[]")
            if len(constraints)!=1:fail('RESTORE_REGISTRY_UNVERIFIED')
            self._psql(entry,'ALTER TABLE '+relation+' DROP CONSTRAINT '+ident(constraints[0])+"; CREATE UNIQUE INDEX restore_jobs_active_schema ON "+relation+"(schema_name) WHERE state IN ('restoring','verified'); COMMENT ON TABLE "+relation+" IS 'wordheat-live:restore-jobs:v2'; SELECT to_json(true)")

        self._psql(entry,"ALTER TABLE wordheat_restore_control.restore_jobs ADD COLUMN IF NOT EXISTS phase text, ADD COLUMN IF NOT EXISTS preserved_namespace jsonb, ADD COLUMN IF NOT EXISTS restored_oid bigint, ADD COLUMN IF NOT EXISTS verified_proof jsonb; SELECT to_json(true)")

    def restore(self,key,archive,manifest):
        with self.target_locked(key) as target:return self._restore_locked(target,archive,manifest)

    def _restore_locked(self,target,archive,manifest):
        if target.database==manifest.get('source_database') or target.instance_id==manifest.get('source_instance_id') or target.source_database!=manifest.get('source_database'):fail('RESTORE_TARGET_REJECTED')
        origins=[entry for entry in self.databases.values() if not entry.restore_target and entry.database==manifest.get('source_database') and entry.instance_id==manifest.get('source_instance_id')]
        if len(origins)!=1 or origins[0].owner==target.owner:fail('RESTORE_SOURCE_UNREGISTERED')
        schema=manifest.get('schema','')
        if not SCHEMA.fullmatch(schema) and not (schema=='public' and manifest.get('source_public') is True):fail('POSTGRES_SCOPE_REJECTED')
        if sha_file(Path(archive))!=manifest.get('sha256'):fail('BACKUP_INTEGRITY_FAILED')
        self.list_archive(archive,expected_schema=schema);self._restore_jobs(target)
        namespaces=self._psql(target,"SELECT coalesce(json_agg(nspname),'[]'::json) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN ('public','information_schema','wordheat_restore_control')")
        public=self._psql(target,"SELECT to_json(EXISTS(SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace) OR EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace) OR EXISTS(SELECT 1 FROM pg_type WHERE typnamespace='public'::regnamespace))")
        if namespaces or public:fail('RESTORE_TARGET_NOT_EMPTY')
        old=self._psql(target,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+literal(manifest['backup_id']))
        if old and (old['archive_sha256']!=manifest['sha256'] or old['namespace_marker']!=manifest['namespace_marker']):fail('RESTORE_REGISTRY_UNVERIFIED')
        sql='INSERT INTO wordheat_restore_control.restore_jobs(backup_id,schema_name,namespace_marker,source_database,source_instance_id,archive_sha256,state) VALUES('+','.join(literal(manifest[x]) for x in ['backup_id','schema','namespace_marker','source_database','source_instance_id','sha256'])+",'restoring') ON CONFLICT(backup_id) DO UPDATE SET state='restoring'; SELECT to_json(true)"
        self._psql(target,sql)
        if schema=='public':self._preserve_empty_public(target,manifest)
        try:
            with Path(archive).open('rb') as stream:self._run('pg_restore',['--host='+self.pg_socket,'--username='+target.owner,'--no-password','--dbname='+target.database,'--no-owner','--no-privileges','--exit-on-error','--single-transaction'],input_file=stream)
            self._psql(target,"UPDATE wordheat_restore_control.restore_jobs SET phase='RESTORE_PROCESS_SUCCEEDED' WHERE backup_id="+literal(manifest['backup_id'])+'; SELECT to_json(true)')
            actual=self._schema_facts(target,schema)
            if (actual['namespace']['marker'] or '')!=manifest['namespace_marker'] or actual['table_counts']!=manifest['table_counts'] or actual['revisions']!=manifest['revisions'] or actual['dataset_sha256']!=manifest['dataset_sha256']:fail('BACKUP_RESTORE_MISMATCH')
            # The restored revision must match installed trusted migration bytes,
            # independently of the backup manifest's claimed revision list.
            self._verify_source_schema_code(target,schema)
            self._mark_owned_namespace(target,manifest)
            transforms=self._secure_restored_instance(target,schema,manifest)
            verified=self._schema_facts(target,schema)
            proof_record={'dataset_sha256':verified['dataset_sha256'],'table_counts':verified['table_counts'],'security_transforms':transforms}
            self._psql(target,'UPDATE wordheat_restore_control.restore_jobs SET state=\'verified\',phase=\'VERIFIED\',verified_proof='+literal(json.dumps(proof_record))+'::jsonb WHERE backup_id='+literal(manifest['backup_id'])+'; SELECT to_json(true)')
            return {'restore_verified':True,'independent_instance_id':target.instance_id,'source_instance_id':manifest['source_instance_id'],'schema':schema,'backup_id':manifest['backup_id'],'archive_sha256':manifest['sha256'],'development_logical_database':True,'business_table_counts_match':True,'security_transforms':transforms}
        except BaseException:
            if schema=='public':self._rollback_empty_public(target,manifest)
            self._psql(target,'UPDATE wordheat_restore_control.restore_jobs SET state=\'failed\' WHERE backup_id='+literal(manifest['backup_id'])+'; SELECT to_json(true)')
            raise

    def cleanup_owned_restore(self,key,schema,namespace_marker,backup_id):
        with self.target_locked(key) as target:return self._cleanup_locked(target,schema,namespace_marker,backup_id)

    def _cleanup_locked(self,target,schema,namespace_marker,backup_id):
        if not SCHEMA.fullmatch(schema) and schema!='public':fail('POSTGRES_SCOPE_REJECTED')
        owned=self._psql(target,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+literal(backup_id))
        actual=self._psql(target,"SELECT json_build_object('marker',obj_description(oid,'pg_namespace'),'owner',pg_get_userbyid(nspowner),'oid',oid::bigint) FROM pg_namespace WHERE nspname="+literal(schema))
        if not owned or owned['schema_name']!=schema or owned['namespace_marker']!=namespace_marker or actual!={'marker':self.job_marker(backup_id,owned['archive_sha256']),'owner':target.owner,'oid':owned['restored_oid']}:fail('RESTORE_CLEANUP_UNOWNED')
        if schema=='public':
            self._validate_preserved_public(target,owned)
            rename='ALTER SCHEMA '+ident(owned['preserved_namespace']['preserved_name'])+' RENAME TO public; '
        else:rename=''
        self._psql(target,'BEGIN; DROP SCHEMA '+ident(schema)+' CASCADE; '+rename+"UPDATE wordheat_restore_control.restore_jobs SET state='cleaned',phase='CLEANED' WHERE backup_id="+literal(backup_id)+'; COMMIT; SELECT to_json(true)')
        if schema=='public':self._assert_original_public(target,owned['preserved_namespace'])

    def _secure_restored_instance(self,target,schema,manifest):
        """Transform only the new owned target; the source archive is unchanged."""
        tables=set(manifest['table_counts']);q=ident(schema)+'.';changes={}
        updates=[]
        if 'service_instance' in tables:
            updates.append('UPDATE '+q+'service_instance SET instance_id='+literal(target.instance_id)+" WHERE id='local'");changes['installation_rebound']=True
        if 'operator_sessions' in tables:
            updates.append('UPDATE '+q+'operator_sessions SET expires_at=least(expires_at,now())');changes['operator_sessions_expired']=True
        if 'display_tickets' in tables:
            updates.append('UPDATE '+q+'display_tickets SET used=true,revoked=true,expires_at=least(expires_at,now())');changes['tickets_revoked']=True
        if 'display_sessions' in tables:
            updates.append('UPDATE '+q+'display_sessions SET revoked=true,expires_at=least(expires_at,now())');changes['display_sessions_revoked']=True
        if 'realtime_connections' in tables:updates.append('UPDATE '+q+'realtime_connections SET ready=false')
        if 'live_sessions' in tables:
            updates.append('UPDATE '+q+"live_sessions SET status='RECOVERY_REQUIRED' WHERE status IN ('ACTIVE','ENDING','RECOVERY_REQUIRED')");changes['sessions_require_recovery']=True
        if 'rounds' in tables:
            updates.append('UPDATE '+q+"rounds SET status=CASE WHEN status='PAUSED' THEN 'PAUSED' ELSE 'FAULTED' END,boot_id="+literal('restored:'+manifest['backup_id'])+",mono_started_ns=0,deadline_at=NULL,drain_until_ns=NULL WHERE status IN ('PREPARING','RUNNING','PAUSED','DRAINING','FAULTED')");changes['old_monotonic_anchors_invalidated']=True
        if 'score_jobs' in tables:updates.append('UPDATE '+q+"score_jobs SET status='cancelled',error_code='RESTORED_INSTANCE_RECOVERY_REQUIRED',lease_until=NULL WHERE status IN ('queued','running')")
        if 'replay_jobs' in tables:updates.append('UPDATE '+q+"replay_jobs SET state='PAUSED',claim_token=NULL,lease_until=NULL WHERE status IN ('queued','running')")
        if 'wordbank_jobs' in tables:updates.append('UPDATE '+q+"wordbank_jobs SET status='failed',error_code='RESTORED_INSTANCE_RECOVERY_REQUIRED',claim_token=NULL,lease_until=NULL WHERE status IN ('queued','running')")
        if 'connector_sessions' in tables:updates.append('UPDATE '+q+"connector_sessions SET state='INTERRUPTED'")
        if 'connector_intents' in tables:updates.append('UPDATE '+q+"connector_intents SET status='failed' WHERE status IN ('pending','running','retry')")
        if 'connector_ack_outbox' in tables:updates.append('UPDATE '+q+"connector_ack_outbox SET status='failed' WHERE status IN ('pending','running','retry')")
        changes['official_device_authorizations_require_new_instance_binding']=True
        if target.runtime_role:
            role=ident(target.runtime_role)
            updates+=['GRANT USAGE ON SCHEMA '+ident(schema)+' TO '+role,'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA '+ident(schema)+' TO '+role,'GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA '+ident(schema)+' TO '+role]
        if target.runtime_role:
            for name in ['service_instance','wordheat_schema_revisions']:
                if name in tables:updates.append('REVOKE INSERT,UPDATE,DELETE ON '+q+ident(name)+' FROM '+ident(target.runtime_role))
        if updates:self._psql(target,'BEGIN; '+'; '.join(updates)+'; COMMIT; SELECT to_json(true)')
        if target.runtime_role:
            role=replace(target,owner=target.runtime_role)
            probe=self._psql(role,'SELECT json_build_object(\'instance_id\',(SELECT instance_id FROM '+q+"service_instance WHERE id='local'),'superuser',rolsuper,'createdb',rolcreatedb,'createrole',rolcreaterole,'public_create',has_schema_privilege(current_user,'public','CREATE')) FROM pg_roles WHERE rolname=current_user")
            if probe!={'instance_id':target.instance_id,'superuser':False,'createdb':False,'createrole':False,'public_create':False}:fail('RESTORE_RUNTIME_ROLE_UNSAFE')
            changes['isolated_runtime_role_verified']=True
        changes['active_configuration_switched']=False
        return changes

    def _dataset_hash(self,entry,schema,tables,*,snapshot=None):
        self.program_identity()
        if not tables:return hashlib.sha256(b'').hexdigest()
        # Stream exact canonical PG row JSON rather than concatenating the dataset in RAM.
        pieces=['SELECT '+literal(name)+" AS name,row_to_json(t)::text AS row FROM "+ident(schema)+'.'+ident(name)+' t' for name in tables]
        sql='SELECT name||chr(9)||row FROM ('+' UNION ALL '.join(pieces)+') rows ORDER BY name,row'
        if snapshot:sql='BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '+literal(snapshot)+'; '+sql+'; COMMIT;'
        sql="SET TIME ZONE 'UTC'; "+sql
        command=[self.docker,'--host',self.endpoint,'exec','--user','postgres','--interactive',self.container_id,'psql','--host='+self.pg_socket,'--username='+entry.owner,'--no-password','--dbname='+entry.database,'--no-psqlrc','--quiet','--tuples-only','--no-align','--set=ON_ERROR_STOP=1','--set=VERBOSITY=sqlstate','--command='+sql]
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        result=hashlib.sha256();deadline=time.monotonic()+60
        try:
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([process.stdout],[],[],remaining)[0]:raise DomainError('POSTGRES_TOOL_FAILED','Data verification timed out',503)
                block=os.read(process.stdout.fileno(),65536)
                if not block:break
                result.update(block)
            if process.wait(timeout=max(.001,deadline-time.monotonic())):raise DomainError('POSTGRES_TOOL_FAILED','Data verification failed',503)
            return result.hexdigest()
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=2)
            process.stdout.close()

    @__import__('contextlib').contextmanager
    def target_locked(self,key):
        target=self.database(key,target=True)
        self.program_identity()
        lock=int.from_bytes(hashlib.sha256(('wordheat-restore:'+target.database).encode()).digest()[:8],'big',signed=True)
        args=[self.docker,'--host',self.endpoint,'exec','--user','postgres','--interactive',self.container_id,'psql','--host='+self.pg_socket,'--username='+target.owner,'--no-password','--dbname='+target.database,'--no-psqlrc','--quiet','--tuples-only','--no-align','--set=ON_ERROR_STOP=1']
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            process.stdin.write(('SELECT CASE WHEN pg_try_advisory_lock('+str(lock)+") THEN 'LOCKED' ELSE 'BUSY' END;\n").encode());process.stdin.flush()
            if not select.select([process.stdout],[],[],5)[0] or process.stdout.readline().strip()!=b'LOCKED':fail('RESTORE_TARGET_BUSY')
            yield target
        finally:
            try:process.stdin.close()
            except OSError:pass
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            process.stdout.close()

    @staticmethod
    def job_marker(backup_id,archive_hash):return 'wordheat-live:restore-job:'+backup_id+':'+archive_hash

    def _namespace_metadata(self,target,name):
        return self._psql(target,"SELECT json_build_object('oid',oid::bigint,'owner',pg_get_userbyid(nspowner),'acl',coalesce(nspacl::text,''),'comment',obj_description(oid,'pg_namespace'),'safe_acl',NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(nspacl,acldefault('n',nspowner))) p WHERE p.privilege_type='CREATE' AND p.grantee<>nspowner)) FROM pg_namespace WHERE nspname="+literal(name))

    def _namespace_empty(self,target,name):
        return self._psql(target,"SELECT to_json(NOT EXISTS(SELECT 1 FROM pg_class WHERE relnamespace="+literal(name)+"::regnamespace) AND NOT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace="+literal(name)+"::regnamespace) AND NOT EXISTS(SELECT 1 FROM pg_type WHERE typnamespace="+literal(name)+"::regnamespace))")

    def _preserve_empty_public(self,target,manifest):
        before=self._namespace_metadata(target,'public')
        if not before or before['owner']!='pg_database_owner' or before['safe_acl'] is not True or not self._namespace_empty(target,'public'):fail('RESTORE_TARGET_NOT_EMPTY')
        preserved='wordheat_restore_empty_'+hashlib.sha256(manifest['backup_id'].encode()).hexdigest()[:24]
        if self._namespace_metadata(target,preserved) is not None:fail('RESTORE_TARGET_NOT_EMPTY')
        before=before|{'preserved_name':preserved}
        sql='BEGIN; UPDATE wordheat_restore_control.restore_jobs SET phase=\'RENAMED\',preserved_namespace='+literal(json.dumps(before))+'::jsonb WHERE backup_id='+literal(manifest['backup_id'])+'; ALTER SCHEMA public RENAME TO '+ident(preserved)+'; COMMIT; SELECT to_json(true)'
        self._psql(target,sql)

    def _mark_owned_namespace(self,target,manifest):
        schema=manifest['schema'];metadata=self._namespace_metadata(target,schema)
        if not metadata or metadata['owner']!=target.owner:fail('RESTORE_OWNERSHIP_UNVERIFIED')
        job=self._psql(target,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+literal(manifest['backup_id']))
        if schema=='public' and (not job['preserved_namespace'] or metadata['oid']==job['preserved_namespace']['oid']):fail('RESTORE_OWNERSHIP_UNVERIFIED')
        marker=self.job_marker(manifest['backup_id'],manifest['sha256'])
        self._psql(target,'BEGIN; COMMENT ON SCHEMA '+ident(schema)+' IS '+literal(marker)+"; UPDATE wordheat_restore_control.restore_jobs SET phase='OWNED_NAMESPACE',restored_oid="+str(metadata['oid'])+' WHERE backup_id='+literal(manifest['backup_id'])+'; COMMIT; SELECT to_json(true)')

    def _validate_preserved_public(self,target,job):
        saved=job.get('preserved_namespace')
        if not saved:fail('RESTORE_RECOVERY_REQUIRED')
        actual=self._namespace_metadata(target,saved['preserved_name'])
        if actual!={k:v for k,v in saved.items() if k!='preserved_name'} or not self._namespace_empty(target,saved['preserved_name']):fail('RESTORE_RECOVERY_REQUIRED')
        return saved

    def _assert_original_public(self,target,saved):
        if self._namespace_metadata(target,'public')!={k:v for k,v in saved.items() if k!='preserved_name'}:fail('RESTORE_RECOVERY_REQUIRED')

    def _rollback_empty_public(self,target,manifest):
        job=self._psql(target,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+literal(manifest['backup_id']))
        if not job or not job.get('preserved_namespace'):return
        if self._namespace_metadata(target,'public') is not None:
            # A committed but unproven namespace is retained; no blind DROP on failure.
            return
        saved=self._validate_preserved_public(target,job)
        self._psql(target,'BEGIN; ALTER SCHEMA '+ident(saved['preserved_name'])+" RENAME TO public; UPDATE wordheat_restore_control.restore_jobs SET phase='RESTORE_ROLLED_BACK' WHERE backup_id="+literal(manifest['backup_id'])+'; COMMIT; SELECT to_json(true)')
        self._assert_original_public(target,saved)

    def verify_owned_restore(self,key,archive,manifest):
        with self.target_locked(key) as target:
            if sha_file(Path(archive))!=manifest['sha256']:fail('BACKUP_INTEGRITY_FAILED')
            job=self._psql(target,'SELECT row_to_json(j) FROM wordheat_restore_control.restore_jobs j WHERE backup_id='+literal(manifest['backup_id']))
            if not job or job['state']!='verified' or job.get('phase')!='VERIFIED' or not job.get('verified_proof') or job['archive_sha256']!=manifest['sha256']:fail('RESTORE_RECOVERY_REQUIRED')
            metadata=self._namespace_metadata(target,manifest['schema'])
            if not metadata or metadata['oid']!=job['restored_oid'] or metadata['owner']!=target.owner or metadata['comment']!=self.job_marker(manifest['backup_id'],manifest['sha256']):fail('RESTORE_RECOVERY_REQUIRED')
            actual=self._schema_facts(target,manifest['schema'])
            if actual['dataset_sha256']!=job['verified_proof']['dataset_sha256'] or actual['table_counts']!=job['verified_proof']['table_counts']:fail('RESTORE_RECOVERY_REQUIRED')
            return {'restore_verified':True,'independent_instance_id':target.instance_id,'source_instance_id':manifest['source_instance_id'],'schema':manifest['schema'],'backup_id':manifest['backup_id'],'archive_sha256':manifest['sha256'],'development_logical_database':True,'business_table_counts_match':True,'security_transforms':job['verified_proof']['security_transforms'],'active_configuration_switched':False}

    @staticmethod
    def _trusted_schema_code():
        # This exact program definition is trusted only because it is installed app
        # migration source, not because the source DB or backup claims to contain it.
        resource=files('migrations.versions').joinpath('20261026-connector-control.sql')
        sql=resource.read_text()
        function=re.search(r'CREATE OR REPLACE FUNCTION wordheat_reject_room_binding_mutation\(\) RETURNS trigger LANGUAGE plpgsql AS \$\$(.*?)\$\$;',sql,re.S)
        trigger='CREATE TRIGGER immutable_room_binding BEFORE UPDATE ON connector_room_bindings FOR EACH ROW EXECUTE FUNCTION wordheat_reject_room_binding_mutation();'
        if not function or trigger not in sql:fail('BACKUP_TRUSTED_CODE_UNAVAILABLE')
        return function.group(1).strip(),hashlib.sha256((function.group(0)+'\n'+trigger).encode()).hexdigest()

    def _verify_source_schema_code(self,entry,schema,*,snapshot=None):
        body,signature=self._trusted_schema_code()
        migration=files('migrations.versions').joinpath('20261026-connector-control.sql')
        expected_revision=hashlib.sha256(migration.read_text().encode()).hexdigest()
        revision=self._psql(entry,'SELECT to_json(sha256) FROM '+ident(schema)+'.wordheat_schema_revisions WHERE revision='+literal(migration.name),snapshot=snapshot)
        if revision!=expected_revision:fail('BACKUP_SCHEMA_MIGRATION_REQUIRED')
        known=self._psql(entry,"SELECT json_build_object('body',p.prosrc,'security_definer',p.prosecdef,'volatile',p.provolatile,'arguments',pg_get_function_identity_arguments(p.oid),'language',l.lanname,'result',pg_get_function_result(p.oid),'config',p.proconfig) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname="+literal(schema)+" AND p.proname='wordheat_reject_room_binding_mutation'",snapshot=snapshot)
        trigger=self._psql(entry,"SELECT json_build_object('table',c.relname,'table_schema',n.nspname,'function',p.proname,'function_schema',f.nspname,'type',t.tgtype::int,'enabled',t.tgenabled,'args',t.tgnargs,'when',t.tgqual IS NOT NULL,'constraint',t.tgconstraint::int) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace f ON f.oid=p.pronamespace WHERE n.nspname="+literal(schema)+" AND t.tgname='immutable_room_binding' AND NOT t.tgisinternal",snapshot=snapshot)
        expected={'body':body,'security_definer':False,'volatile':'v','arguments':'','language':'plpgsql','result':'trigger','config':None}
        if not known or (known|{'body':known['body'].strip()})!=expected:fail('BACKUP_TRUSTED_CODE_MISMATCH')
        if trigger!={'table':'connector_room_bindings','table_schema':schema,'function':'wordheat_reject_room_binding_mutation','function_schema':schema,'type':19,'enabled':'O','args':0,'when':False,'constraint':0}:fail('BACKUP_TRUSTED_CODE_MISMATCH')

    def _archive_selected_sql(self,archive,selector,schema):
        with Path(archive).open('rb') as stream:
            raw=self._run('pg_restore',['--schema-only','--no-owner','--no-privileges','--no-comments','--file=-','--schema='+schema,selector],input_file=stream)
        try:return raw.decode('utf-8')
        except UnicodeError:fail('BACKUP_TOC_REJECTED')

    @staticmethod
    def _selected_statement(sql):
        # Fixed pg_restore emits a standard prologue. No archive statement other
        # than the one exact selected CREATE is permitted in this extracted slice.
        guards=re.findall(r'^\\restrict ([A-Za-z0-9]{1,128})$',sql,re.M)
        ends=re.findall(r'^\\unrestrict ([A-Za-z0-9]{1,128})$',sql,re.M)
        if guards or ends:
            if len(guards)!=1 or ends!=guards:fail('BACKUP_TRUSTED_CODE_MISMATCH')
            # A fixed pg_restore plain-output safety wrapper is not archive SQL.
            # We never execute this extraction; binary restore remains the executor.
            sql=re.sub(r'^\\(?:un)?restrict '+re.escape(guards[0])+r'\s*$','',sql,flags=re.M)
        lines=[];dollar=None
        for line in sql.splitlines():
            if dollar is None and (not line.strip() or line.lstrip().startswith('--')):continue
            lines.append(line)
            # Keep every body byte, including comments, for the installed-body
            # equality check; only pg_restore's outer comments are discarded.
            for match in re.finditer(r'\$[A-Za-z0-9_]*\$',line):
                tag=match.group(0)
                if dollar is None:dollar=tag
                elif dollar==tag:dollar=None
        if dollar is not None:fail('BACKUP_TRUSTED_CODE_MISMATCH')
        text='\n'.join(lines).strip()
        prologue=[r'SET statement_timeout = 0;',r'SET lock_timeout = 0;',r'SET idle_in_transaction_session_timeout = 0;',r"SET client_encoding = 'UTF8';",r'SET standard_conforming_strings = on;',r"SELECT pg_catalog.set_config\('search_path', '', false\);",r'SET check_function_bodies = false;',r'SET xmloption = content;',r'SET client_min_messages = warning;',r'SET row_security = off;']
        for pattern in prologue:text=re.sub(r'^'+pattern+r'\s*','',text,count=1)
        return text.strip()

    def _verify_archive_schema_code(self,archive,schema,toc):
        has_function=any(re.match(r'^\d+; \d+ \d+ FUNCTION ',line) for line in toc)
        has_trigger=any(re.match(r'^\d+; \d+ \d+ TRIGGER ',line) for line in toc)
        # Current restore supports the installed schema only. Historical archives
        # require an explicit forward-migration compatibility path; their own
        # metadata cannot opt out of a required installed security invariant.
        if not has_function and not has_trigger:fail('BACKUP_SCHEMA_MIGRATION_REQUIRED')
        if not has_function or not has_trigger:fail('BACKUP_TRUSTED_CODE_MISMATCH')
        body,signature=self._trusted_schema_code()
        function_sql=self._selected_statement(self._archive_selected_sql(archive,'--function=wordheat_reject_room_binding_mutation()',schema))
        name=re.escape(schema)
        pattern=r'CREATE FUNCTION "?'+name+r'"?\.wordheat_reject_room_binding_mutation\(\) RETURNS trigger\s+LANGUAGE plpgsql\s+AS (\$[A-Za-z0-9_]*\$)(.*?)\1;'
        matched=re.fullmatch(pattern,function_sql,re.S)
        if not matched or matched.group(2).strip()!=body:fail('BACKUP_TRUSTED_CODE_MISMATCH')
        trigger_sql=self._selected_statement(self._archive_selected_sql(archive,'--trigger=connector_room_bindings immutable_room_binding',schema))
        expected='CREATE TRIGGER immutable_room_binding BEFORE UPDATE ON '+schema+'.connector_room_bindings FOR EACH ROW EXECUTE FUNCTION '+schema+'.wordheat_reject_room_binding_mutation();'
        if trigger_sql!=expected:fail('BACKUP_TRUSTED_CODE_MISMATCH')

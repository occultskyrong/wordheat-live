"""Private native maintenance port. JSON or Web booleans are not execution evidence."""
from dataclasses import dataclass
from pathlib import Path
from app.errors import DomainError
from app.modules.operations.runtime_artifacts import sha_file

@dataclass(frozen=True)
class NativeStageEvidence:
    stage:str
    command_id:str
    plan_fingerprint:str
    result:dict
    _key:object

class NativeMaintenancePort:
    def __init__(self,registry,executor_factory=None):
        self.registry=registry;self._key=object()
        def attest(stage,command_id,plan_fingerprint,result):
            return NativeStageEvidence(stage,command_id,plan_fingerprint,result,self._key)
        self.executor=executor_factory(attest) if executor_factory else None

    def prepare(self,operation,artifact_id,command_id,current,**options):
        return self.registry.prepare(operation,artifact_id,command_id,current,**options)

    def restore_plan(self,payload,expected):return self.registry.restore_plan(payload,expected)

    def require_evidence(self,value,stage,plan):
        if not isinstance(value,NativeStageEvidence) or value._key is not self._key or value.stage!=stage or value.command_id!=plan.payload['command_id'] or value.plan_fingerprint!=plan.fingerprint:
            raise DomainError('NATIVE_EXECUTION_UNVERIFIED','Private native execution evidence required',403)
        return value.result

    def run_stage(self,stage,plan,prior):
        self.registry.recheck(plan)
        if self.executor is None:raise DomainError('POSTGRES_TOOL_CONFIGURATION_REQUIRED' if plan.payload['operation'] in {'backup_local','verify_local_backup','restore_new_instance'} else 'NATIVE_EXECUTOR_UNAVAILABLE','Fixed registered maintenance executor is not configured',503)
        value=self.executor.run_stage(stage,plan,prior)
        result=self.require_evidence(value,stage,plan)
        if stage=='CREATE_BACKUP':
            archive=result.get('archive',{})
            path=self.registry.path(archive.get('path',''))
            if path.stat().st_size!=archive.get('bytes') or sha_file(path)!=archive.get('sha256'):
                raise DomainError('BACKUP_INTEGRITY_FAILED','Created backup bytes do not match evidence',409)
            with path.open('rb') as stream:
                if stream.read(5)!=b'PGDMP':raise DomainError('BACKUP_FORMAT_INVALID','PostgreSQL custom archive required',409)
        if stage=='VALIDATE_BACKUP' and result.get('archive_validated') is not True:
            raise DomainError('BACKUP_FORMAT_INVALID','Native archive parser must validate the custom archive',409)
        if stage=='VERIFY_RESTORE' and not (result.get('restore_verified') is True and result.get('independent_instance_id') and result['independent_instance_id']!=plan.payload['installation_id']):
            raise DomainError('BACKUP_RESTORE_UNVERIFIED','Independent restore evidence required',409)
        return result

    def reconcile(self,stage,plan,prior):
        self.registry.recheck(plan)
        if self.executor is None or not callable(getattr(self.executor,'reconcile',None)):
            raise DomainError('MAINTENANCE_RECOVERY_REQUIRED','Native stage outcome needs explicit reconciliation',409)
        return self.require_evidence(self.executor.reconcile(stage,plan,prior),stage,plan)

"""Standard API composition: persistent core, control plane and owned lifecycles."""
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import threading
from app.main import create_app as create_core_app
from app.errors import DomainError
from app.modules.analytics.service import AnalyticsService
from app.modules.analytics.router import create_router as analytics_router
from app.modules.analytics.maintenance import AnalyticsMaintenanceWorker
from app.http_contract import typed_analytics_router
from app.infrastructure.runtime_configuration import (roots,private_directory,gate_roots,release_keys,connector_registry,DeviceCredentials,LazyPostgresTools,RegisteredBackupExecutor,SignedCandidateResolver,SignedControlImporter)
from app.modules.operations.trusted_control import TrustedControl
from app.modules.connectors.coordinator import OfficialCoordinator
from app.modules.connectors.transport import RelayControlPort,ConnectorSupervisor
from app.modules.connectors.pairing import NativePairingClient
from app.modules.connectors.native_pairing_bridge import NativePairingBridge
from app.modules.operations.runtime_router import bind_runtime,LocalServerStop
from app.modules.operations.runtime_controller import PrivateRuntimeBoot
from app.modules.operations.runtime_artifacts import ArtifactRegistry
from app.modules.operations.runtime_native import NativeMaintenancePort


class ApplicationTrustedControl(TrustedControl):
    def authorize_create(self,c,actor,body):
        if getattr(self.db,'test_namespace',None) is not None and body.get('run_purpose')=='production':raise DomainError('TEST_NAMESPACE_PRODUCTION_FORBIDDEN','Isolated test namespaces cannot authorize production sessions',403)
        return super().authorize_create(c,actor,body)
    def require_current(self,c,session):
        if getattr(self.db,'test_namespace',None) is not None and session.get('run_purpose')=='production':raise DomainError('TEST_NAMESPACE_PRODUCTION_FORBIDDEN','Isolated test namespaces cannot commit production results',403)
        return super().require_current(c,session)


def create_app(settings,scorer=None,display_probe=None,*,runtime_stop_port=None,runtime_boot_context=None,source_root=None,program_entry=None):
    key=getattr(settings,'analytics_cursor_key',None)
    if not isinstance(key,bytes) or len(key)!=32:raise DomainError('ANALYTICS_CONFIG_INVALID','Dedicated analytics cursor key missing or invalid',503)
    app=create_core_app(settings,scorer=scorer,display_probe=display_probe)
    core=app.state.service;db=app.state.database
    if runtime_boot_context is not None:
        context=runtime_boot_context
        if not isinstance(context,PrivateRuntimeBoot) or context.installation_id!=settings.instance_id or getattr(core.scorer,'manifest_id',None)!=context.model_manifest_id or getattr(core.scorer,'manifest_sha256',None)!=context.model_manifest_sha256:raise DomainError('RUNTIME_BOOT_IDENTITY_INVALID','Verified private runtime bootstrap required',403)
        core.boot_id=context.boot_id
        app.state.wordbank_worker.boot=context.boot_id
    analytics=AnalyticsService(db,key);core.analytics=analytics;app.state.analytics=analytics
    app.include_router(typed_analytics_router(analytics_router(db,app.state.auth,analytics,key)))
    # Paths are not created or credentials read while merely constructing routers.
    configured_root=getattr(settings,'server_root',None)
    resolved=roots(settings,source_root,program_entry) if configured_root is not None else None
    source=resolved['source_root'] if resolved else source_root or getattr(settings,'source_root',None)
    candidate=SignedCandidateResolver(settings,core,source)
    credentials=DeviceCredentials(getattr(settings,'device_private_key',None),getattr(settings,'device_pairing_id',None),getattr(settings,'device_credential_file',None))
    registry={}
    trust=ApplicationTrustedControl(db,candidate_digest=candidate.current_digest,component_inventory={},boot_id=core.boot_id)
    port=RelayControlPort(registry,credentials,test_mode=getattr(settings,'environment','development')=='test')
    coordinator=OfficialCoordinator(db,trust,control_port=port,hooks=core,registry=registry,observer=analytics.observe_delivery)
    coordinator.registry=registry
    core.official=coordinator;core.command_resolvers.append(coordinator.lookup_command)
    core.readiness.trusted_control=trust;core.readiness.connector=coordinator
    analytics.connector_retention=coordinator.store.prune_local_payloads
    core.connector_trace_metadata=coordinator.store.trace_metadata
    supervisor=ConnectorSupervisor(coordinator,port)
    maintenance=AnalyticsMaintenanceWorker(analytics)
    app.state.device_credentials=credentials
    app.state.trusted_control=trust;app.state.connector=coordinator;app.state.connector_supervisor=supervisor
    app.state.connector_task=None;app.state.connector_stopping=None
    app.state.analytics_maintenance_worker=maintenance;app.state.candidate_resolver=candidate
    app.state.configuration_status={}
    stop_port=runtime_stop_port or LocalServerStop();app.state.runtime_stop_port=stop_port
    original=app.router.lifespan_context
    @asynccontextmanager
    async def background(value):
        async with original(value):
            await maintenance.start()
            await candidate.start()
            stopping=asyncio.Event();app.state.connector_stopping=stopping
            task=asyncio.create_task(supervisor.run(stopping));app.state.connector_task=task
            try:yield
            finally:
                stopping.set()
                try:await asyncio.wait_for(asyncio.shield(task),timeout=7)
                except TimeoutError:task.cancel();await asyncio.gather(task,return_exceptions=True)
                await candidate.stop();await maintenance.stop();credentials.clear()
    app.router.lifespan_context=background
    pairing_client=NativePairingClient(core,trust,registry,test_mode=getattr(settings,'environment','development')=='test',on_paired=lambda result:credentials.install_verified(result.credential))
    pairing=NativePairingBridge(core,env_path=resolved['server_root']/'.env',control_root=resolved['control_root'],pairing_client=pairing_client,credential_provider=credentials) if resolved else None
    if pairing is not None:core.command_resolvers.append(pairing.lookup_command)
    app.state.native_pairing_bridge=pairing
    controller=bind_runtime(app,stop_port=stop_port,platform_projection=coordinator.stop_projection,boot_id=core.boot_id,bundle_issuer=core.bundle_permit_issuer(),resource_issuer=core.bundle_resource_permit_issuer(),control_importer=SignedControlImporter(trust),boot_context=runtime_boot_context,pairing_bridge=pairing,native_root=resolved['native_root'] if resolved else None,source_root=source,program_entry=resolved['program_entry'] if resolved else None,configuration_root=resolved['server_root'] if resolved else None)
    bounded=app.router.lifespan_context
    initialized=False
    initialize_lock=threading.Lock()
    def initialize_configuration():
        nonlocal initialized
        with initialize_lock:
            if initialized:return
            current=roots(settings,source_root,program_entry)
            for label in ('native_root','maintenance_root','control_root'):private_directory(current[label])
            # Optional official material never promotes local mode or establishes
            # gates. An invalid optional store disables that capability explicitly.
            for name,load in [('gate_roots',lambda:gate_roots(getattr(settings,'gate_trust_roots_file',None))),('connectors',lambda:connector_registry(getattr(settings,'connector_registry_file',None),settings.environment))]:
                try:
                    loaded=load()
                    if name=='gate_roots':trust.keys=loaded
                    else:registry.clear();registry.update(loaded)
                    app.state.configuration_status[name]='configured' if loaded else 'not_configured'
                except (DomainError,ValueError) as error:
                    if name=='gate_roots':trust.keys={}
                    else:registry.clear()
                    app.state.configuration_status[name]=getattr(error,'code','SERVER_CONFIGURATION_INVALID')
            try:credentials.initialize();app.state.configuration_status['device']='configured' if credentials.value is not None else 'not_configured'
            except (DomainError,ValueError) as error:credentials.clear();app.state.configuration_status['device']=getattr(error,'code','DEVICE_CREDENTIAL_CONFIGURATION_INVALID')
            try:public=release_keys(getattr(settings,'release_public_keys_file',None));app.state.configuration_status['release_roots']='configured' if public else 'not_configured'
            except (DomainError,ValueError) as error:public={};app.state.configuration_status['release_roots']=getattr(error,'code','RELEASE_ROOT_CONFIGURATION_INVALID')
            artifacts=ArtifactRegistry(current['maintenance_root'],public,settings.instance_id,control_root=current['control_root'])
            tools=LazyPostgresTools(getattr(settings,'maintenance_registry_file',None),settings.instance_id)
            native=NativeMaintenancePort(artifacts,lambda attest:RegisteredBackupExecutor(attest,tools=tools,registry=artifacts,core=core))
            controller.native_port=native;app.state.maintenance_registry=artifacts;app.state.postgres_tools=tools
            initialized=True
    @asynccontextmanager
    async def lifecycle(value):
        # Full application startup requires an explicit root. The configuration
        # phase has no open SQL transaction and performs no platform/network IO.
        roots(settings,source_root,program_entry)
        await asyncio.to_thread(db.verify_runtime)
        await asyncio.to_thread(initialize_configuration)
        async with bounded(value):yield
    app.router.lifespan_context=lifecycle
    return app

"""Standard relay server composition; deployment allowlists never issue authority."""
import base64
from contextvars import ContextVar
from pathlib import Path
from psycopg import sql
from psycopg.rows import dict_row
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .core import RelayError,RelayService,UnavailableProvider,create_app,safe_connect,validate_database,migrate
from .control import RelayControlPlane,SignedPairingManagementVerifier,FreshnessSigner,strict


def verify_namespace(connection,config,*,migration=False):
    if config.namespace=='relay':return
    row=connection.execute("SELECT obj_description(oid) AS marker,pg_get_userbyid(nspowner) AS owner,current_user AS role FROM pg_namespace WHERE nspname=%s",(config.namespace,)).fetchone()
    if not row or row['marker']!=config.namespace_marker or (migration and row['owner']!=row['role']):raise RelayError('TEST_NAMESPACE_IDENTITY_MISMATCH',503)


class ConfiguredControlPlane(RelayControlPlane):
    """Per-call candidate context preserves all inherited signed-scope checks.

    The immutable cached deployment policy only filters candidate digests. It
    cannot create gates, keys, grants, release approval or platform provenance.
    """
    def __init__(self,service,config):
        self.policy=config.candidates
        self._candidate=ContextVar('relay_allowed_candidate',default=None)
        signer=FreshnessSigner(config.signer_key_id,Ed25519PrivateKey.from_private_bytes(config.signer_seed)) if config.signer_seed is not None else None
        super().__init__(service,trusted_keys=config.roots,signer=signer,candidate_digest=self._candidate.get)
        self.management=ConfiguredPairingVerifier(self)

    def verify(self,credential):
        try:
            if not isinstance(credential,str) or len(credential)>16384:raise ValueError()
            request=strict(base64.urlsafe_b64decode(credential+'='*((-len(credential))%4)))
            if set(request)!={'device_token','authorization_id','session_id'} or any(not isinstance(v,str) or not v for v in request.values()):raise ValueError()
        except (ValueError,TypeError):raise RelayError('AUTH_REQUIRED',403) from None
        with self.service.tx() as c:
            rows=c.execute("SELECT payload->>'candidate_digest' AS digest FROM relay.trusted_objects WHERE type='live_grant' AND payload->>'authorization_id'=%s",(request['authorization_id'],)).fetchall()
        if len(rows)!=1 or not self.policy.permits(rows[0]['digest']):raise RelayError('CANDIDATE_MISMATCH',403)
        token=self._candidate.set(rows[0]['digest'])
        try:return super().verify(credential)
        finally:self._candidate.reset(token)

    def owner_failure(self,c,owner,now):
        if owner.get('authorization_id') and not self.policy.permits(owner.get('candidate_digest')):return 'CANDIDATE_MISMATCH'
        token=self._candidate.set(owner.get('candidate_digest'))
        try:return super().owner_failure(c,owner,now)
        finally:self._candidate.reset(token)


class ConfiguredPairingVerifier(SignedPairingManagementVerifier):
    def verify(self,credential):
        try:
            if not isinstance(credential,str) or len(credential)>16384:raise ValueError()
            obj=strict(base64.urlsafe_b64decode(credential+'='*((-len(credential))%4)))
            digest=obj['payload']['candidate_digest']
        except (ValueError,TypeError,KeyError):raise RelayError('AUTH_REQUIRED',403) from None
        if not self.plane.policy.permits(digest):raise RelayError('CANDIDATE_MISMATCH',403)
        token=self.plane._candidate.set(digest)
        try:return super().verify(credential)
        finally:self.plane._candidate.reset(token)


def build_server(config):
    settings=config.settings
    with safe_connect(settings.database_url,row_factory=dict_row) as c:
        validate_database(c,settings.database_name,settings.database_marker)
        verify_namespace(c,config)
    service=RelayService(settings.database_url,expected_database=settings.database_name,expected_marker=settings.database_marker,schema=config.namespace)
    plane=ConfiguredControlPlane(service,config)
    platform=None;provider=None
    if settings.douyin_app_id and settings.douyin_app_secret and settings.douyin_request_profile:
        from .provider import DouyinClient,DouyinPlatformAdapter,DouyinPushProvider,DouyinRoomVerifier,RSAProfile
        rsa=None
        if settings.douyin_request_profile=='openapi_rsa':
            try:rsa=RSAProfile(settings.douyin_app_id,settings.douyin_rsa_key_version,base64.b64decode(settings.douyin_rsa_private_key_b64,validate=True),base64.b64decode(settings.douyin_platform_public_key_b64,validate=True))
            except Exception:raise RelayError('DOUYIN_RSA_CONFIGURATION_REQUIRED',503) from None
        client=DouyinClient(settings.douyin_app_id,settings.douyin_app_secret,profile=settings.douyin_request_profile,rsa_profile=rsa,allow_network=settings.douyin_external_calls_enabled)
        platform=DouyinPlatformAdapter(client,scope_guard=service.task_scope_guard);plane.rooms=DouyinRoomVerifier(client)
        if settings.douyin_push_secret and settings.douyin_identity_key_b64url:
            from .server_config import _key
            identity=_key(settings.douyin_identity_key_b64url)
            provider=DouyinPushProvider(settings.douyin_app_id,settings.douyin_push_secret,identity,provenance=lambda app,room:plane.ingress_provenance(app,room,channel_profile=settings.douyin_channel_profile,channel_id=settings.douyin_channel_id,request_profile=settings.douyin_request_profile))
    app=create_app(service,provider=provider or UnavailableProvider(),grant_verifier=plane,test_mode=settings.synthetic_test_enabled,control_plane=plane,platform_adapter=platform,run_workers=True)
    @app.get('/relay/v1/health/live')
    def live():return {'status':'alive'}
    app.state.configured_control=plane
    return app


def migrate_server(config):
    if config.namespace=='relay':
        return migrate(config.database_url,expected_database=config.database_name,expected_marker=config.database_marker,runtime_role=config.runtime_role)
    # Test-only explicit existing namespace, owned by this migration role. This
    # path cannot create arbitrary schemas or touch the canonical relay schema.
    with safe_connect(config.database_url,row_factory=dict_row) as c:
        validate_database(c,config.database_name,config.database_marker,True);verify_namespace(c,config,migration=True)
        c.execute('SELECT pg_advisory_xact_lock(786542391)')
        for path in sorted((Path(__file__).parents[1]/'migrations').glob('*.sql')):
            text=path.read_text().replace('CREATE SCHEMA IF NOT EXISTS relay;','').replace('relay.',config.namespace+'.')
            c.execute(text)
        namespace=sql.Identifier(config.namespace);role=sql.Identifier(config.runtime_role)
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(namespace,role))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA {} TO {}').format(namespace,role))
        c.execute(sql.SQL('REVOKE DELETE ON ALL TABLES IN SCHEMA {} FROM {}').format(namespace,role))
        c.execute(sql.SQL('REVOKE INSERT,UPDATE ON {}.schema_versions FROM {}').format(namespace,role))

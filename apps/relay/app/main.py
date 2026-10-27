"""Server composition entrypoint. Production adapters must be injected by trusted code."""
from .config import RelaySettings
from .core import RelayService, create_app,UnavailableProvider,UnavailableGrantVerifier


def build_app(settings: RelaySettings, *, provider=None, grant_verifier=None,control_plane_factory=None,platform_adapter=None):
    service=RelayService(settings.database_url,expected_database=settings.database_name,expected_marker=settings.database_marker)
    plane=control_plane_factory(service) if control_plane_factory else None
    if settings.douyin_app_id and settings.douyin_app_secret and settings.douyin_request_profile:
        from .provider import DouyinClient,DouyinPlatformAdapter,DouyinPushProvider,DouyinRoomVerifier,RSAProfile
        from .control import SignedPairingManagementVerifier
        import base64
        rsa_profile=None
        if settings.douyin_request_profile=='openapi_rsa':
            try:rsa_profile=RSAProfile(settings.douyin_app_id,settings.douyin_rsa_key_version,base64.b64decode(settings.douyin_rsa_private_key_b64,validate=True),base64.b64decode(settings.douyin_platform_public_key_b64,validate=True))
            except Exception:
                from .core import RelayError
                raise RelayError('DOUYIN_RSA_CONFIGURATION_REQUIRED',503) from None
        client=DouyinClient(settings.douyin_app_id,settings.douyin_app_secret,profile=settings.douyin_request_profile,rsa_profile=rsa_profile,allow_network=settings.douyin_external_calls_enabled)
        platform_adapter=platform_adapter or DouyinPlatformAdapter(client,scope_guard=service.task_scope_guard)
        if plane:
            plane.management=plane.management or SignedPairingManagementVerifier(plane);plane.rooms=plane.rooms or DouyinRoomVerifier(client)
            if provider is None and settings.douyin_push_secret and settings.douyin_identity_key_b64url:
                try:
                    encoded=settings.douyin_identity_key_b64url;identity=base64.urlsafe_b64decode(encoded+'='*((4-len(encoded)%4)%4))
                    if len(identity)!=32 or base64.urlsafe_b64encode(identity).rstrip(b'=').decode()!=encoded:raise ValueError()
                except Exception:
                    from .core import RelayError
                    raise RelayError('DOUYIN_IDENTITY_KEY_CONFIGURATION_REQUIRED',503) from None
                provider=DouyinPushProvider(settings.douyin_app_id,settings.douyin_push_secret,identity,provenance=lambda app,room:plane.ingress_provenance(app,room,channel_profile=settings.douyin_channel_profile,channel_id=settings.douyin_channel_id,request_profile=settings.douyin_request_profile))
    return create_app(service,provider=provider or UnavailableProvider(),grant_verifier=grant_verifier or plane or UnavailableGrantVerifier(),test_mode=settings.synthetic_test_enabled,control_plane=plane,platform_adapter=platform_adapter,run_workers=True)

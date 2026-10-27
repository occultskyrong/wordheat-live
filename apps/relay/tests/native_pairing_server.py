"""Owned test-only server. Fake upstream via MockTransport, no external calls."""
import base64,json,socket,sys,threading,time
from pathlib import Path
import httpx
import uvicorn
from conftest import relay_environment
from app.core import create_app,UnavailableProvider
from app.control import RelayControlPlane,SignedPairingManagementVerifier
from app.provider import DouyinClient,DouyinRoomVerifier


def main():
    info=json.loads(Path(sys.argv[1]).read_text());encoded=info['public_key'];public=base64.urlsafe_b64decode(encoded+'='*((4-len(encoded)%4)%4))
    roots={info['key_id']:{'public_key':public,'domains':['wordheat-live.readiness'],'types':['pairing_intent'],'test_only':True}}
    def fake(request):
        if request.url.path.endswith('/token'):return httpx.Response(200,json={'err_no':0,'data':{'access_token':'synthetic-room-token','expires_in':7200}})
        assert request.url.path=='/api/webcastmate/info' and json.loads(request.content)['token']=='synthetic-launch'
        return httpx.Response(200,json={'data':{'info':{'room_id':info['room_id'],'anchor_open_id':'synthetic-anchor'}}})
    fixture=relay_environment.__wrapped__();service,grant,lease,clock=next(fixture)
    plane=RelayControlPlane(service,trusted_keys=roots,candidate_digest=info['candidate'],room_verifier=DouyinRoomVerifier(DouyinClient('synthetic-pair-app','synthetic-pairing-sdk-secret',profile='data_open_access_token',transport=httpx.MockTransport(fake))))
    plane.management=SignedPairingManagementVerifier(plane)
    app=create_app(service,provider=UnavailableProvider(),grant_verifier=plane,control_plane=plane,test_mode=True)
    owned=socket.socket();owned.bind(('127.0.0.1',0));owned.listen(16)
    server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',ws='websockets-sansio',log_level='critical'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[owned]},daemon=True);thread.start()
    try:
        until=time.monotonic()+5
        while not server.started:
            if time.monotonic()>until:raise RuntimeError('Owned test relay startup failed')
            time.sleep(.01)
        print(json.dumps({'port':owned.getsockname()[1]}),flush=True)
        sys.stdin.readline()
    finally:
        server.should_exit=True;thread.join(5);owned.close()
        try:next(fixture)
        except StopIteration:pass
    return 0


if __name__=='__main__':raise SystemExit(main())

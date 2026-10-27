"""Measured local persistence, not Windows/OBS or platform G2 evidence."""
import json,os,socket,threading,time,math
from pathlib import Path
from datetime import datetime,timedelta,timezone
import httpx
import uvicorn
from app.core import create_app
from app.provider import DouyinPushProvider,ChannelProvenance,push_signature


def test_public_comment_batch_1000_is_atomic_and_response_capacity_is_recorded(relay_environment):
    service,grant,lease,clock=relay_environment
    policy=lambda app,room:ChannelProvenance('controlled_test_live',True,'synthetic-capacity-contract',datetime.now(timezone.utc)+timedelta(minutes=5))
    provider=DouyinPushProvider(grant.app_id,'synthetic-capacity-secret',b'c'*32,provenance=policy)
    provider.provider_test=True  # Contract fixture, never an official channel pass.
    class Verifier:
        def verify(self,value):return grant
    app=create_app(service,provider=provider,grant_verifier=Verifier(),test_mode=True)
    owned=socket.socket();owned.bind(('127.0.0.1',0));owned.listen(16);port=owned.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,loop='asyncio',ws='websockets-sansio',log_level='critical'));thread=threading.Thread(target=server.run,kwargs={'sockets':[owned]},daemon=True);thread.start()
    durations=[];last_raw=None;last_h=None
    try:
        until=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<until;time.sleep(.01)
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,follow_redirects=False,timeout=5) as client:
            for batch in range(8):
                payload=[{'msg_id':f'synthetic-{batch}-{i}','sec_openid':f'synthetic-user-{i}','content':'测试','nickname':'Synthetic viewer','avatar_url':'not-fetched','timestamp':int(time.time()*1000)} for i in range(1000)]
                raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode();h={'x-nonce-str':'synthetic-'+str(batch),'x-timestamp':str(int(time.time()*1000)),'x-roomid':grant.room_id,'x-msg-type':'live_comment','content-type':'application/json'};h['x-signature']=push_signature(h,raw,'synthetic-capacity-secret')
                started=time.perf_counter();response=client.post('/relay/v1/douyin/callback',content=raw,headers=h);durations.append((time.perf_counter()-started)*1000)
                assert response.status_code==204 and not response.content
                last_raw,last_h=raw,h
            with service.tx() as c:
                before=c.execute('SELECT last_seq FROM relay.relay_streams WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['last_seq']
            assert client.post('/relay/v1/douyin/callback',content=last_raw,headers=last_h).status_code==204
            # Same source changes anywhere reject the complete batch without
            # consuming a sequence, so the unmodified batch remains retryable.
            bad=json.loads(last_raw);bad[-1]['content']='改变';raw=json.dumps(bad,ensure_ascii=False,separators=(',',':')).encode();h=last_h|{'x-signature':push_signature(last_h,raw,'synthetic-capacity-secret')}
            assert client.post('/relay/v1/douyin/callback',content=raw,headers=h).status_code==409
            with service.tx() as c:assert c.execute('SELECT last_seq FROM relay.relay_streams WHERE app_id=%s AND room_id=%s',(grant.app_id,grant.room_id)).fetchone()['last_seq']==before
        ordered=sorted(durations);report={'scope':'synthetic real-loopback HTTP + dedicated owned PostgreSQL namespace','batch_messages':1000,'samples':len(ordered),'p50_ms':ordered[math.ceil(.5*len(ordered))-1],'p95_ms':ordered[math.ceil(.95*len(ordered))-1],'p99_ms':ordered[-1],'samples_ms':durations,'target_p95_ms':300,'target_p99_ms':1000,'official_g0_or_g2':False,'includes_windows_obs':False}
        if os.environ.get('WORDHEAT_RELAY_BENCH_REPORT'):Path(os.environ['WORDHEAT_RELAY_BENCH_REPORT']).write_text(json.dumps(report,indent=2))
        assert len(ordered)==8  # Targets are reported, never fabricated by a lenient threshold.
    finally:server.should_exit=True;thread.join(5);owned.close();assert not thread.is_alive()

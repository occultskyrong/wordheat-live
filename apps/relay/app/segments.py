"""Explicit new-scene bootstrap; old namespace watermarks and bytes stay intact."""
from uuid import uuid4
from psycopg.types.json import Jsonb
from .core import RelayError,opaque_id


def bootstrap(service,app,room,lease,*,expected_stream_id,command_id,reason):
    opaque_id(command_id,128);opaque_id(expected_stream_id)
    if reason!='EXPLICIT_FRESH_SESSION':raise RelayError('INPUT_INVALID',422)
    with service.tx() as c:
        o,w,st,now=service.lock(c,app,room);service.expire(c,o,w,st,now);service.authorize(o,lease,now)
        existing=c.execute('SELECT * FROM relay.stream_segments WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND bootstrap_command_id=%s',(app,room,o['installation_id'],o['session_id'],command_id)).fetchone()
        if existing:
            if st['stream_id']!=existing['stream_id']:raise RelayError('STREAM_BOOTSTRAP_STALE')
            return service.lease_view(o,c)|{'fresh_start':True}
        if st['stream_id']!=expected_stream_id:raise RelayError('STREAM_ID_CONFLICT')
        if w and w['state']!='CLOSED':raise RelayError('OLD_WINDOW_STILL_OPEN')
        old=c.execute('SELECT * FROM relay.stream_segments WHERE stream_id=%s FOR UPDATE',(st['stream_id'],)).fetchone()
        if old and old['session_id']==o['session_id']:
            played=c.execute('SELECT 1 FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_id=%s AND eligible LIMIT 1',(app,room,st['stream_id'])).fetchone()
            budget=c.execute('SELECT 1 FROM relay.authorization_sessions WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s',(app,room,o['installation_id'],o['session_id'])).fetchone()
            if played or budget:raise RelayError('NEW_SESSION_REQUIRED')
            if old['previous_stream_id'] is not None:return service.lease_view(o,c)|{'fresh_start':True}
        cp=c.execute('SELECT * FROM relay.relay_checkpoints WHERE app_id=%s AND room_id=%s AND installation_id=%s FOR UPDATE',(app,room,o['installation_id'])).fetchone()
        persisted=cp['persisted_seq'] if cp and cp['stream_id']==st['stream_id'] else 0
        rpo={'previous_stream_id':st['stream_id'],'previous_session_id':old['session_id'] if old else None,'previous_installation_id':old['installation_id'] if old else None,'persisted_seq':str(persisted),'retained_from':str(st['retained_from']),'unprocessed_from':str(persisted+1) if persisted<st['last_seq'] else None,'unprocessed_through':str(st['last_seq']) if persisted<st['last_seq'] else None,'expired_gap_from':str(persisted+1) if persisted<st['retained_from']-1 else None,'expired_gap_through':str(st['retained_from']-1) if persisted<st['retained_from']-1 else None,'old_history_reconstructed':False}
        if cp and cp['stream_id'] and cp['session_id']:
            c.execute('INSERT INTO relay.checkpoint_history(app_id,room_id,installation_id,session_id,stream_id,persisted_seq,delivered_seq) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id,installation_id,session_id,stream_id) DO UPDATE SET persisted_seq=greatest(relay.checkpoint_history.persisted_seq,excluded.persisted_seq),delivered_seq=greatest(relay.checkpoint_history.delivered_seq,excluded.delivered_seq)',(app,room,o['installation_id'],cp['session_id'],cp['stream_id'],cp['persisted_seq'],cp['delivered_seq']))
        new=str(uuid4());start=st['last_seq']+1;previous=st['stream_id']
        c.execute('UPDATE relay.stream_segments SET end_seq=%s WHERE stream_id=%s',(st['last_seq'],previous))
        c.execute('INSERT INTO relay.stream_segments(stream_id,app_id,room_id,start_seq,installation_id,session_id,owner_generation,previous_stream_id,bootstrap_command_id,reason,rpo) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',(new,app,room,start,o['installation_id'],o['session_id'],o['owner_generation'],previous,command_id,reason,Jsonb(rpo)))
        c.execute('UPDATE relay.relay_streams SET stream_id=%s,retained_from=%s WHERE app_id=%s AND room_id=%s',(new,start,app,room))
        c.execute('UPDATE relay.room_owners SET connection_nonce=NULL WHERE app_id=%s AND room_id=%s',(app,room))
        c.execute('INSERT INTO relay.relay_checkpoints(app_id,room_id,installation_id,persisted_seq,delivered_seq,connection_nonce,stream_id,session_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(app_id,room_id,installation_id) DO UPDATE SET persisted_seq=excluded.persisted_seq,delivered_seq=excluded.delivered_seq,connection_nonce=excluded.connection_nonce,stream_id=excluded.stream_id,session_id=excluded.session_id',(app,room,o['installation_id'],start-1,start-1,str(uuid4()),new,o['session_id']))
        st['stream_id']=new;st['retained_from']=start
        seq=service.append(c,o,None,st,now,'stream.bootstrap',{'be_extension_version':'LW-BE/1.1.0','start_seq':str(start),'previous_stream_id':previous,'rpo':rpo})
        c.execute('UPDATE relay.stream_segments SET bootstrap_seq=%s WHERE stream_id=%s',(seq,new))
        return service.lease_view(o,c)|{'fresh_start':True}

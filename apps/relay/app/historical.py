"""Authenticated historical ACKs are cleanup facts, never new live authority."""
import base64
import hashlib
from datetime import timezone
import rfc8785
from .core import RelayError,decimal_cursor,opaque_id
from .control import strict,verify_envelope,stamp
from .trust_keys import historical_keys


def accept(plane,credential,body,*,intent_id=None):
    try:
        request=strict(base64.urlsafe_b64decode(credential+'='*((4-len(credential)%4)%4)))
        if set(request)!={'device_token','authorization_id','session_id'}:raise ValueError()
    except (ValueError,TypeError):raise RelayError('AUTH_REQUIRED',403) from None
    required={'stream_id','stream_seq','stage','receipt_id'}
    if not required<=set(body) or not set(body)<=required|{'retry_failed','client_time'} or type(body.get('retry_failed',False)) is not bool or body['stage'] not in ('transport.persisted','display.received','display.processed'):raise RelayError('INPUT_INVALID',422)
    opaque_id(body['stream_id']);opaque_id(body['receipt_id']);seq=decimal_cursor(body['stream_seq']);service=plane.service
    with service.tx() as c:
        c.execute('SELECT pg_advisory_xact_lock_shared(786542396)')
        pair=plane.device(c,request['device_token'],service.clock())
        source=c.execute('SELECT * FROM relay.delivery_contexts WHERE stream_id=%s AND stream_seq=%s',(body['stream_id'],seq)).fetchone()
        if not source:raise RelayError('SOURCE_SCOPE_UNAVAILABLE',403)
        context=source['context']
        if any(context.get(k)!=pair[k] for k in ('app_id','room_id','installation_id')) or context['session_id']!=request['session_id']:raise RelayError('INSTALLATION_MISMATCH',403)
        if context.get('device_binding_id') and context['device_binding_id']!=pair['pairing_id']:
            original=c.execute('SELECT * FROM relay.device_pairings WHERE pairing_id=%s',(context['device_binding_id'],)).fetchone()
            if not original or original['status']!='bound' or original['revoked_at'] or bytes(original['device_public_key'])!=bytes(pair['device_public_key']) or any(original[k]!=pair[k] for k in ('app_id','room_id','installation_id')):raise RelayError('DEVICE_AUTHORIZATION_REVOKED',403)
        if context.get('authorization_id'):
            if request['authorization_id']!=context['authorization_id']:raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
            grant=c.execute("SELECT envelope,revoked FROM relay.trusted_objects WHERE type='live_grant' AND digest=%s AND payload->>'authorization_id'=%s",(context['grant_digest'],context['authorization_id'])).fetchone()
            if not grant:raise RelayError('SOURCE_SCOPE_UNAVAILABLE',403)
            keys=plane.effective_keys(c);verified=verify_envelope(grant['envelope'],keys,expected_type='live_grant')
            _,revoked=plane.revocation_state(c,keys)
            if grant['revoked'] or verified['payload']['object_id'] in revoked or verified['payload'].get('revoked_at'):raise RelayError('LIVE_AUTHORIZATION_REVOKED',403)
            if any(verified['payload'].get(k)!=context.get(k) for k in ('app_id','room_binding_id','installation_id','candidate_digest')):raise RelayError('LIVE_AUTHORIZATION_INVALID',403)
        elif not pair['provider_test']:raise RelayError('SOURCE_SCOPE_UNAVAILABLE',403)
        app=source['app_id'];room=source['room_id']
        # The room lock serializes a namespace switch with checkpoint history.
        c.execute('SELECT 1 FROM relay.room_owners WHERE app_id=%s AND room_id=%s FOR UPDATE',(app,room))
        if body['stage']=='transport.persisted':
            cp=c.execute('SELECT * FROM relay.checkpoint_history WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND stream_id=%s FOR UPDATE',(app,room,pair['installation_id'],request['session_id'],body['stream_id'])).fetchone()
            current=False
            if not cp:
                cp=c.execute('SELECT * FROM relay.relay_checkpoints WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND stream_id=%s FOR UPDATE',(app,room,pair['installation_id'],request['session_id'],body['stream_id'])).fetchone();current=True
            if not cp or seq>cp['delivered_seq']:raise RelayError('ACK_OUT_OF_RANGE')
            if seq>cp['persisted_seq']:
                c.execute("INSERT INTO relay.delivery_stages SELECT app_id,room_id,stream_seq,'transport.persisted',%s,%s FROM relay.relay_frames WHERE app_id=%s AND room_id=%s AND stream_id=%s AND stream_seq>%s AND stream_seq<=%s ON CONFLICT DO NOTHING",(body['receipt_id'],service.clock(),app,room,body['stream_id'],cp['persisted_seq'],seq))
                table='relay.relay_checkpoints' if current else 'relay.checkpoint_history'
                c.execute('UPDATE '+table+' SET persisted_seq=%s WHERE app_id=%s AND room_id=%s AND installation_id=%s AND session_id=%s AND stream_id=%s',(seq,app,room,pair['installation_id'],request['session_id'],body['stream_id']))
        else:
            actual_time=stamp(body.get('client_time'))
            if source['event_type']!='comment' or not source['eligible']:raise RelayError('DISPLAY_SOURCE_DELIVERY_REQUIRED')
            persisted=c.execute("SELECT 1 FROM relay.delivery_stages WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND stage='transport.persisted'",(app,room,seq)).fetchone()
            if not persisted:raise RelayError('ACK_OUT_OF_RANGE')
            if body['stage']=='display.processed' and not c.execute("SELECT 1 FROM relay.delivery_stages WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND stage='display.received'",(app,room,seq)).fetchone():raise RelayError('DISPLAY_RECEIVED_REQUIRED')
            existing=c.execute('SELECT payload FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND kind=%s',(app,room,seq,body['stage'])).fetchone()
            if existing:
                # HTTP and WS share one immutable report intent. Compare the
                # actual SDK millisecond before recording a retry or acceptance.
                try:
                    previous=stamp(existing['payload'].get('client_time')).astimezone(timezone.utc)
                    current=actual_time.astimezone(timezone.utc)
                except (RelayError,ValueError,TypeError,AttributeError,OverflowError):raise RelayError('RECEIPT_CONFLICT') from None
                if previous.replace(microsecond=previous.microsecond//1000*1000)!=current.replace(microsecond=current.microsecond//1000*1000):raise RelayError('RECEIPT_CONFLICT')
            c.execute('INSERT INTO relay.delivery_stages VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',(app,room,seq,body['stage'],body['receipt_id'],service.clock()))
            service.enqueue_job(c,context,body['stage'],stream_seq=seq,payload={'receipt_id':body['receipt_id'],'client_time':actual_time.isoformat()})
            actual=c.execute('SELECT job_id FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND stream_seq=%s AND kind=%s',(app,room,seq,body['stage'])).fetchone()
            service.retry_job(c,actual['job_id'],intent_id,body.get('retry_failed',False))
        pending=c.execute("SELECT count(*) AS n FROM relay.relay_outbox WHERE app_id=%s AND room_id=%s AND payload->'context'->>'session_id'=%s AND status NOT IN ('succeeded','cancelled')",(app,room,request['session_id'])).fetchone()['n']
        return {'status':'accepted','stage':body['stage'],'stream_id':body['stream_id'],'stream_seq':str(seq),'remote_fulfillment_pending':pending if service.platform_adapter_configured else None,'provider_test':context.get('provider_test',False)}

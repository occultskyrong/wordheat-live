"""Short-transaction native receive and independent delivery acknowledgement state."""
import base64
from datetime import datetime,timezone,timedelta
from uuid import uuid4
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from app.errors import DomainError
from . import models as m
from .protocol import decode_delivery,cursor


class ConnectorStore:
    def __init__(self,database,observer=None):self.db=database;self.observer=observer

    def binding(self,c,sid):
        row=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid).with_for_update()).mappings().first()
        if not row:raise DomainError('CONNECTOR_NOT_READY','Connector session is not prepared',409)
        return dict(row)

    def observe(self,c,sid):
        state=self.binding(c,sid)
        revision=state['revision']+1
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(revision=revision))
        if self.observer:
            count=c.execute(sa.select(sa.func.count()).select_from(m.acks).where(m.acks.c.session_id==sid,m.acks.c.status.in_(['pending','running','retry']))).scalar_one()
            self.observer(c,sid,'connector:'+sid,revision,ack_outbox=count,remote_fulfillment_pending=state['remote_pending'],relay_coverage='unknown' if state['state']=='GAP' else 'complete' if state['terminal_seq']==state['persisted_seq'] else 'partial',relay_confirmed_stream_seq=None)

    def persist(self,c,sid,frame,connection):
        body=decode_delivery(frame,connection);state=self.binding(c,sid)
        seq=cursor(body['stream_seq']);stream=body['stream_id']
        if state['stream_id'] and stream!=state['stream_id']:raise DomainError('RELAY_GAP','Stream identity changed',409)
        if seq>state['persisted_seq']+1024:raise DomainError('RELAY_GAP','Receive gap exceeds bounded buffer',409)
        existing=c.execute(sa.select(m.deliveries).where(m.deliveries.c.stream_id==stream,m.deliveries.c.stream_seq==seq)).mappings().first()
        changed=existing is None
        if existing:
            if existing['immutable_sha256']!=frame['immutable_sha256'] or existing['session_id']!=sid:raise DomainError('FRAME_INTEGRITY_FAILURE','Stream sequence content changed',409)
        else:
            if seq<=state['persisted_seq']:raise DomainError('RELAY_GAP','Previously persisted frame missing',409)
            c.execute(m.deliveries.insert().values(id=str(uuid4()),session_id=sid,stream_id=stream,stream_seq=seq,immutable_sha256=frame['immutable_sha256'],raw_body=base64.b64decode(frame['body_b64']),body=body,state='persisted'))
        upto=state['persisted_seq']
        rows=c.execute(sa.select(m.deliveries.c.stream_seq).where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_id==stream,m.deliveries.c.stream_seq>upto).order_by(m.deliveries.c.stream_seq).limit(1024)).scalars()
        for number in rows:
            if number!=upto+1:break
            upto=number
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(stream_id=stream,persisted_seq=upto))
        if upto>state['persisted_seq']:self.queue_ack(c,sid,stream,upto,'transport.persisted',str(uuid4()))
        if changed or upto>state['persisted_seq']:self.observe(c,sid)
        return upto

    def queue_ack(self,c,sid,stream,seq,stage,receipt_id,client_time=None):
        if stage not in ('transport.persisted','display.received','display.processed'):raise DomainError('INPUT_INVALID','Unknown acknowledgement stage',422)
        row=c.execute(sa.select(m.acks).where(m.acks.c.session_id==sid,m.acks.c.stream_id==stream,m.acks.c.stream_seq==seq,m.acks.c.stage==stage)).mappings().first()
        if row:return row['id']
        key=str(uuid4())
        c.execute(m.acks.insert().values(id=key,session_id=sid,stream_id=stream,stream_seq=seq,stage=stage,receipt_id=receipt_id,status='pending',client_time=client_time))
        c.execute(m.bindings.update().where(m.bindings.c.session_id==sid).values(remote_pending=None))
        return key

    def receipt(self,c,sid,stream,seq,stage,receipt_id,client_time=None):
        if stage not in ('display.received','display.processed'):raise DomainError('INPUT_INVALID','Invalid display stage',422)
        state=self.binding(c,sid)
        source=c.execute(sa.select(m.deliveries).where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_id==stream,m.deliveries.c.stream_seq==seq)).mappings().first()
        if not source or seq>state['persisted_seq'] or source['body'].get('type')!='comment' or source['body'].get('eligible') is not True:raise DomainError('DISPLAY_SOURCE_DELIVERY_REQUIRED','Receipt source was not durably received',409)
        from app.modules.auth.evidence import parse_time
        if client_time is None:raise DomainError('INPUT_INVALID','Actual display client_time is required',422)
        actual_time=parse_time(client_time)
        if stage=='display.processed' and not c.execute(sa.select(m.acks.c.id).where(m.acks.c.session_id==sid,m.acks.c.stream_id==stream,m.acks.c.stream_seq==seq,m.acks.c.stage=='display.received')).first():raise DomainError('DISPLAY_RECEIVED_REQUIRED','Actual received evidence is required first',409)
        result=self.queue_ack(c,sid,stream,seq,stage,receipt_id,actual_time);self.observe(c,sid);return result

    def advance(self,c,sid,seq,*,terminal=False,reason=None):
        state=self.binding(c,sid)
        if seq>state['persisted_seq'] or seq>state['admitted_seq']+1:raise DomainError('RELAY_GAP','Admission sequence is not continuous',409)
        c.execute(m.deliveries.update().where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_seq==seq).values(state='terminal' if terminal else 'admitted',reason_code=reason))
        admitted=max(state['admitted_seq'],seq);terminal_seq=state['terminal_seq']
        rows=c.execute(sa.select(m.deliveries.c.stream_seq,m.deliveries.c.state).where(m.deliveries.c.session_id==sid,m.deliveries.c.stream_seq>terminal_seq,m.deliveries.c.stream_seq<=admitted).order_by(m.deliveries.c.stream_seq)).all()
        for number,status in rows:
            if number!=terminal_seq+1 or status not in ('terminal','archived','rejected'):break
            terminal_seq=number
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(admitted_seq=admitted,terminal_seq=terminal_seq))
        self.observe(c,sid)

    def mark_gap(self,c,sid,reason):
        state=self.binding(c,sid)
        c.execute(m.bindings.update().where(m.bindings.c.id==state['id']).values(state='GAP',last_error=reason))
        self.observe(c,sid)

    @staticmethod
    def prune_local_payloads(c,sid,cutoff):
        """Caller holds session lock and has independently verified final-backed seal."""
        safe_before=min(cutoff,datetime.now(timezone.utc)-timedelta(days=7))
        state=c.execute(sa.select(m.bindings).where(m.bindings.c.session_id==sid).with_for_update()).mappings().first()
        if not state or state['state'] not in ('STOPPED','INTERRUPTED','GAP'):return 0
        pending=sa.exists(sa.select(m.acks.c.id).where(m.acks.c.session_id==sid,m.acks.c.stream_id==m.deliveries.c.stream_id,m.acks.c.stream_seq==m.deliveries.c.stream_seq,m.acks.c.status.in_(['pending','running','retry'])))
        rows=c.execute(sa.select(m.deliveries).where(m.deliveries.c.session_id==sid,m.deliveries.c.created_at<safe_before,m.deliveries.c.state.in_(['terminal','archived','rejected']),m.deliveries.c.raw_body.is_not(None),~pending).with_for_update()).mappings().all()
        for row in rows:
            c.execute(m.deliveries.update().where(m.deliveries.c.id==row['id']).values(raw_body=None,body=row['body']|{'payload':{}}))
        return len(rows)

    @staticmethod
    def trace_metadata(c,message_id):
        row=c.execute(sa.select(m.deliveries.c.stream_id,m.deliveries.c.stream_seq).where(m.deliveries.c.feedback_id==message_id)).mappings().first()
        return {'stream_id':row['stream_id'],'stream_seq':str(row['stream_seq'])} if row else None

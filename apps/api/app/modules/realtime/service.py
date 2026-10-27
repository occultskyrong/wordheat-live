"""Persistent effects/grants and audience streams. No synthetic processed ACKs."""
import hashlib,json
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid,now

CONTRACT='LW-C1/1.0.0'
def stamp(): return now().isoformat(timespec='milliseconds').replace('+00:00','Z')

class RealtimeService:
    def __init__(self,service): self.service=service; self.db=service.db

    def transport(self,sid,actor=None):
        with self.db.read_snapshot() as c:
            session=self.service.session(c,sid,actor,lock=False)
            return self.service.transport(c,session)

    def is_ready(self,*,session_id,display_instance_id):
        with self.db.engine.connect() as c:
            return self.is_ready_in_transaction(c,session_id=session_id,display_instance_id=display_instance_id)

    def is_ready_in_transaction(self,c,*,session_id,display_instance_id):
        # Callers already holding session/round locks must retain their existing
        # connection: a second checkout can wait on contenders for those locks.
        return bool(c.execute(sa.select(s.connections.c.id).join(s.displays,s.displays.c.id==s.connections.c.display_id).where(s.displays.c.id==display_instance_id,s.displays.c.session_id==session_id,s.displays.c.revoked.is_(False),s.displays.c.expires_at>now(),s.connections.c.boot_id==self.service.boot_id,s.connections.c.ready.is_(True))).first())

    def pending(self,c,sid):
        return list(c.execute(sa.select(s.effects.c.payload).where(s.effects.c.session_id==sid,s.effects.c.processed.is_(False)).order_by(s.effects.c.source_seq,s.effects.c.id).limit(128)).scalars())

    def reserve(self,c,display,generation):
        inflight=c.execute(sa.select(sa.func.count()).select_from(s.grants).where(s.grants.c.display_id==display['id'],s.grants.c.generation==generation,s.grants.c.processed.is_(False))).scalar_one()
        if inflight>=128: return []
        already=sa.select(s.grants.c.effect_id).where(s.grants.c.display_id==display['id'],s.grants.c.generation==generation)
        pending=list(c.execute(sa.select(s.effects).where(s.effects.c.session_id==display['session_id'],s.effects.c.processed.is_(False),s.effects.c.id.not_in(already)).order_by(s.effects.c.source_seq,s.effects.c.id).limit(128-inflight)).mappings())
        for effect in pending: c.execute(s.grants.insert().values(id=uid(),display_id=display['id'],effect_id=effect['id'],generation=generation))
        return [effect['payload'] for effect in pending]

    def feedback_payload(self,c,sid,payload,audience):
        feedbacks=[f for f in payload.get('feedbacks',[]) if f['status']!='queued']
        clean=[{k:f[k] for k in ('feedback_id','word','display_name','status','display_score','solved','message')} for f in feedbacks]
        if audience!='DISPLAY': return {'feedbacks':clean,'effects':[]}
        current_seq=c.execute(sa.select(s.sessions.c.display_seq).where(s.sessions.c.id==sid)).scalar_one()
        for feedback in feedbacks:
            if c.execute(sa.select(s.effects.c.id).where(s.effects.c.feedback_id==feedback['feedback_id'])).first(): continue
            guess=c.execute(sa.select(s.guesses).where(s.guesses.c.id==feedback['feedback_id'])).mappings().first()
            if not guess: guess=c.execute(sa.select(s.rejections).where(s.rejections.c.id==feedback['feedback_id'])).mappings().first()
            if not guess or not guess['round_id']: continue
            epoch=c.execute(sa.select(s.rounds.c.epoch).where(s.rounds.c.id==guess['round_id'])).scalar_one()
            delivery=uid(); effect={'feedback_id':feedback['feedback_id'],'origin_round_id':guess['round_id'],'window_epoch':str(epoch),'delivery_ids':[delivery],'outcome':feedback['status'],'status_message':feedback['message'],'word':feedback['word'],'display_name':feedback['display_name'],'display_score':feedback['display_score'],'solved':feedback['solved']}
            c.execute(s.effects.insert().values(id=uid(),session_id=sid,round_id=guess['round_id'],feedback_id=feedback['feedback_id'],delivery_id=delivery,source_seq=current_seq+1,payload=effect))
        display=c.execute(sa.select(s.displays).join(s.connections,s.connections.c.display_id==s.displays.c.id).where(s.displays.c.session_id==sid,s.displays.c.purpose=='primary',s.displays.c.revoked.is_(False),s.displays.c.expires_at>now(),s.connections.c.ready.is_(True),s.connections.c.boot_id==self.service.boot_id)).mappings().first()
        granted=self.reserve(c,display,display['render_generation']) if display else []
        return {'feedbacks':clean,'effects':payload.get('effects',[])+granted}

    def check_display(self,c,display_id,sid=None):
        display=c.execute(sa.select(s.displays).where(s.displays.c.id==display_id)).mappings().first()
        if not display or display['revoked'] or display['expires_at']<=now() or (sid and display['session_id']!=sid): raise DomainError('DISPLAY_UNAUTHORIZED','Display authorization expired',403)
        return display

    def connect(self,display_id,sid):
        with self.db.engine.begin() as c:
            self.service.session(c,sid)
            display=self.check_display(c,display_id,sid)
            nonce=uid(); generation=display['render_generation']+1
            c.execute(s.displays.update().where(s.displays.c.id==display_id).values(render_generation=generation))
            c.execute(s.connections.delete().where(s.connections.c.display_id==display_id))
            c.execute(s.connections.insert().values(id=uid(),display_id=display_id,boot_id=self.service.boot_id,nonce=nonce,generation=generation,ready=False))
            self.service.touch(c,sid)
            return nonce,generation

    def disconnect(self,display_id,nonce):
        with self.db.engine.begin() as c:
            display=c.execute(sa.select(s.displays).where(s.displays.c.id==display_id)).mappings().first()
            if not display: return
            self.service.session(c,display['session_id'])
            c.execute(s.connections.update().where(s.connections.c.display_id==display_id,s.connections.c.nonce==nonce).values(ready=False)); self.service.touch(c,display['session_id'])

    def active(self,display_id,nonce):
        with self.db.engine.connect() as c:
            display=self.check_display(c,display_id)
            if not c.execute(sa.select(s.connections.c.id).where(s.connections.c.display_id==display_id,s.connections.c.nonce==nonce,s.connections.c.boot_id==self.service.boot_id)).first(): raise DomainError('DISPLAY_UNAUTHORIZED','Connection replaced',403)
            return dict(display)

    def replay(self,sid,audience,after,actor=None):
        with self.db.engine.begin() as c:
            session=self.service.session(c,sid,actor)
            upto=session['display_seq' if audience=='DISPLAY' else 'studio_seq']
            events=list(c.execute(sa.select(s.outbox).where(s.outbox.c.session_id==sid,s.outbox.c.audience==audience,s.outbox.c.seq>after,s.outbox.c.seq<=upto).order_by(s.outbox.c.seq).limit(256)).mappings())
            if after>upto or len(events)!=min(256,upto-after) or any(row['seq']!=after+i+1 for i,row in enumerate(events)): raise DomainError('RELAY_GAP','Audience event gap',409,details={'min_seq':str(events[0]['seq'] if events else upto),'latest_seq':str(upto)})
            return [dict(row) for row in events],events[-1]['seq'] if events else after

    def at_boundary(self,sid,audience,after,actor=None):
        with self.db.engine.begin() as c:
            session=self.service.session(c,sid,actor)
            return session['display_seq' if audience=='DISPLAY' else 'studio_seq']==after

    def ready(self,display_id,nonce,upto):
        with self.db.engine.begin() as c:
            display=self.check_display(c,display_id); session=self.service.session(c,display['session_id'])
            conn=c.execute(sa.select(s.connections).where(s.connections.c.display_id==display_id,s.connections.c.nonce==nonce)).mappings().first()
            if not conn: raise DomainError('DISPLAY_UNAUTHORIZED','Connection replaced',403)
            if session['display_seq']!=upto: return None
            effects=self.reserve(c,display,conn['generation']) if display['purpose']=='primary' else []
            c.execute(s.connections.update().where(s.connections.c.id==conn['id']).values(ready=True)); self.service.touch(c,display['session_id'])
            return {'kind':'resume_ready','schema_version':1,'contract_version':CONTRACT,'session_id':display['session_id'],'upto_seq':str(upto),'server_time':stamp(),'render_generation':str(conn['generation']),'pending_effects':effects}

    def receipt(self,display_id,body):
        with self.db.engine.begin() as c:
            display=self.check_display(c,display_id); session=self.service.session(c,display['session_id'])
            if display['purpose']!='primary' or body['display_instance_id']!=display_id or int(body['render_generation'])!=display['render_generation']: raise DomainError('DISPLAY_UNAUTHORIZED','Current primary generation required',403)
            conn=c.execute(sa.select(s.connections).where(s.connections.c.display_id==display_id,s.connections.c.generation==display['render_generation'],s.connections.c.ready.is_(True),s.connections.c.boot_id==self.service.boot_id)).mappings().first()
            if not conn: raise DomainError('DISPLAY_UNAUTHORIZED','Current ready connection required',403)
            digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            old=c.execute(sa.select(s.receipts).where(s.receipts.c.display_id==display_id,s.receipts.c.generation==display['render_generation'],s.receipts.c.batch_id==body['batch_id'],s.receipts.c.stage==body['stage'])).mappings().first()
            if old:
                if old['body_hash']!=digest: raise DomainError('IDEMPOTENCY_KEY_REUSED','Receipt batch changed',409)
                return old['response']
            for delivery in body['deliveries']:
                effect=c.execute(sa.select(s.effects).where(s.effects.c.delivery_id==delivery['delivery_id'],s.effects.c.feedback_id==delivery['feedback_id'],s.effects.c.session_id==display['session_id'])).mappings().first()
                grant=c.execute(sa.select(s.grants).where(s.grants.c.display_id==display_id,s.grants.c.generation==display['render_generation'],s.grants.c.effect_id==effect['id'])).mappings().first() if effect else None
                if not grant: raise DomainError('DISPLAY_UNAUTHORIZED','Effect was not granted',403)
                if body['stage']=='processed' and not grant['received']: raise DomainError('INPUT_INVALID','processed requires received evidence',422)
                c.execute(s.grants.update().where(s.grants.c.id==grant['id']).values(**{body['stage']:True}))
                if body['stage']=='processed': c.execute(s.effects.update().where(s.effects.c.id==effect['id']).values(processed=True))
            result={'batch_id':body['batch_id'],'stage':body['stage'],'accepted':len(body['deliveries'])}
            c.execute(s.receipts.insert().values(id=uid(),display_id=display_id,generation=display['render_generation'],batch_id=body['batch_id'],stage=body['stage'],body_hash=digest,response=result))
            if session['mode']=='official' and self.service.official is not None:
                self.service.official.record_display_receipt(c,display,body)
            if body['stage']=='processed':
                effects=self.reserve(c,display,display['render_generation'])
                if effects: self.service.event(c,display['session_id'],'guess.feedback',{'feedbacks':[],'effects':effects},audiences=('DISPLAY',))
            self.service.touch(c,display['session_id'])
            return result

    @staticmethod
    def envelope(event,mode,allow_effects=False,extended=False):
        payload=dict(event['payload'])
        if event['type']=='guess.feedback' and (mode=='replay' or not allow_effects): payload['effects']=[]
        if not extended and event['audience']=='STUDIO' and event['type']=='command.updated':
            command=dict(payload['command']); command.pop('be_extension_version',None)
            if isinstance(command.get('state'),dict) and 'selection_id' in command['state']: command['state']={'status':command['state'].get('status','UPDATED')}
            payload={'command':command}
        effects=payload.get('effects',[])
        rid=payload.get('round',{}).get('round_id') or payload.get('reveal',{}).get('round_id') or (effects[0]['origin_round_id'] if effects else None)
        return {'kind':'event','schema_version':1,'contract_version':CONTRACT,'event_id':event['id'],'session_id':event['session_id'],'round_id':rid,'seq':str(event['seq']),'server_time':event['created_at'].isoformat(timespec='milliseconds').replace('+00:00','Z'),'type':event['type'],'delivery_ids':[d for e in effects for d in e['delivery_ids']],'delivery_mode':mode,'payload':payload}

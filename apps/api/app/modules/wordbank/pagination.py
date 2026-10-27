"""Authenticated keyset pages over a frozen, server-captured insert visibility set."""
import base64
import hashlib
import hmac
import json
from datetime import datetime,timedelta
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s
from app.modules.auth.service import now
from app.modules.wordbank.service import puzzle_dto

def invalid(): return DomainError('PUZZLE_CURSOR_INVALID','Invalid puzzle cursor or query binding',422)
DOMAIN=b'wordheat:puzzles-cursor:1\0'

def encode(key,payload):
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw+hmac.digest(key,DOMAIN+raw,'sha256')).decode().rstrip('=')

def decode(key,token,actor,filters,size):
    try:
        if not isinstance(token,str) or not 1<=len(token)<=4096: raise ValueError()
        raw=base64.b64decode(token+'='*((4-len(token)%4)%4),altchars=b'-_',validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip('=')!=token or len(raw)<=32: raise ValueError()
        body,signature=raw[:-32],raw[-32:]
        if not hmac.compare_digest(signature,hmac.digest(key,DOMAIN+body,'sha256')): raise ValueError()
        value=json.loads(body)
        if value['version']!=1 or value['actor']!=actor or value['filters']!=filters or value['size']!=size: raise ValueError()
        if datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))<=now(): raise ValueError()
        if not isinstance(value['last'],list) or len(value['last'])!=3 or not isinstance(value['last'][0],str) or type(value['last'][1]) is not int or not isinstance(value['last'][2],str): raise ValueError()
        if type(value['page']) is not int or value['page']<1 or type(value['total']) is not int or value['total']<0: raise ValueError()
        return value
    except Exception: raise invalid() from None

def page(wordbank,c,actor,filters,size,number,cursor):
    if cursor and number!=1: raise invalid()
    state=decode(wordbank.paging_key,cursor,actor,filters,size) if cursor else None
    if state:
        visibility=state['visibility']; as_of=state['as_of']; expires=state['expires_at']; number=state['page']+1
    else:
        observed=c.execute(sa.text('SELECT pg_current_snapshot()::text AS visibility, transaction_timestamp() AS captured_at')).mappings().one()
        visibility=observed['visibility']; as_of=observed['captured_at'].isoformat(timespec='milliseconds').replace('+00:00','Z')
        expires=(now()+timedelta(minutes=15)).isoformat(timespec='milliseconds').replace('+00:00','Z')
    # Unlike created_at, PostgreSQL visibility excludes versions from a writer
    # that began before page 1 but committed after its captured snapshot.
    visible=sa.text('pg_visible_in_snapshot(puzzle_versions.xmin::text::xid8, CAST(:visibility AS pg_snapshot))')
    query=sa.select(s.puzzles).where(visible)
    if not filters['all_versions']:
        latest=sa.select(s.puzzles.c.puzzle_id,sa.func.max(s.puzzles.c.version).label('latest')).where(visible).group_by(s.puzzles.c.puzzle_id).subquery()
        query=query.join(latest,sa.and_(latest.c.puzzle_id==s.puzzles.c.puzzle_id,latest.c.latest==s.puzzles.c.version))
    if filters['category_id']: query=query.where(s.puzzles.c.category_id==filters['category_id'])
    if filters['status']: query=query.where(s.puzzles.c.status==filters['status'])
    if filters['q']: query=query.where(s.puzzles.c.word.contains(filters['q'],autoescape=True))
    query=query.where(s.puzzles.c.difficulty.between(filters['difficulty_min'],filters['difficulty_max']))
    if filters['difficulty'] is not None: query=query.where(s.puzzles.c.difficulty==filters['difficulty'])
    query=query.params(visibility=visibility)
    selected=query.subquery()
    identity=selected.c.id+sa.literal(':')+sa.cast(selected.c.version,sa.Text)+sa.literal(':')+selected.c.status
    count,fingerprint=c.execute(sa.select(sa.func.count(),sa.func.coalesce(sa.func.bit_xor(sa.func.hashtextextended(identity,0)),0)).select_from(selected)).one()
    # In-place status/review mutations cannot be reconstructed from an insert
    # snapshot. Reject a changed read set instead of silently omitting a row.
    if state and (state['total']!=count or state['fingerprint']!=str(fingerprint)):
        raise DomainError('PUZZLE_CURSOR_STALE','Puzzle read set changed; request a new first page',409)
    if state:
        pid,version,identity=state['last']
        query=query.where(sa.or_(s.puzzles.c.puzzle_id>pid,sa.and_(s.puzzles.c.puzzle_id==pid,s.puzzles.c.version<version),sa.and_(s.puzzles.c.puzzle_id==pid,s.puzzles.c.version==version,s.puzzles.c.id>identity)))
    elif number>1:
        query=query.offset((number-1)*size)  # Explicit legacy offset read only.
    rows=list(c.execute(query.order_by(s.puzzles.c.puzzle_id,s.puzzles.c.version.desc(),s.puzzles.c.id).limit(size+1)).mappings())
    more=len(rows)>size; next_cursor=None
    if more:
        last=rows[size-1]
        next_cursor=encode(wordbank.paging_key,{'version':1,'actor':actor,'filters':filters,'size':size,'visibility':visibility,'as_of':as_of,'expires_at':expires,'last':[last['puzzle_id'],last['version'],last['id']],'page':number,'total':count,'fingerprint':str(fingerprint)})
    return {'items':[puzzle_dto(row) for row in rows[:size]],'page':number,'page_size':size,'total':count,'next_cursor':next_cursor,'has_more':more,'as_of':as_of,'expires_at':expires}

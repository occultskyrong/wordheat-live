"""Catalog mutations share the outer global lock and ordered session locks."""
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.wordbank import selection
from app.errors import DomainError
from app.modules.auth.service import uid


def disable(service,c,actor,puzzle_id,body):
    selection.catalog_lock(c)
    sessions=list(c.execute(sa.select(s.sessions).where(s.sessions.c.status!='ENDED').order_by(s.sessions.c.id).with_for_update()).mappings())
    puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.puzzle_id==puzzle_id).order_by(s.puzzles.c.version.desc()).with_for_update()).mappings().first()
    if not puzzle: raise DomainError('RESOURCE_NOT_FOUND','Puzzle not found',404)
    service.check_version(puzzle,body['expected_version'])
    if body['command']!='disable' or not isinstance(body.get('args',{}).get('reason'),str) or not 1<=len(body['args']['reason'].strip())<=200: raise DomainError('INPUT_INVALID','disable requires reason',422)
    c.execute(s.puzzles.update().where(s.puzzles.c.puzzle_id==puzzle_id).values(status='disabled'))
    revision=c.execute(s.catalog.update().where(s.catalog.c.id=='global').values(revision=s.catalog.c.revision+1).returning(s.catalog.c.revision)).scalar_one()
    for session in sessions:
        session=service.session(c,session['id'])
        pointer=selection.pointer(c,session['id'])
        old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==pointer['selection_id'])).mappings().first() if pointer['selection_id'] else None
        selected=c.execute(sa.select(s.puzzles.c.puzzle_id).where(s.puzzles.c.id==old['puzzle_version_id'])).scalar_one_or_none() if old and old['puzzle_version_id'] else None
        if old and (old['status']=='EMPTY' or (old['status']=='READY' and selected==puzzle_id)):
            service.reselect(c,session,'PUZZLE_DISABLED',f"catalog:{revision}",content={'puzzle_id':puzzle_id,'catalog_revision':revision})
    return {'command_id':uid(),'status':'applied','resource_version':puzzle['version'],'state':'disabled','error':None}

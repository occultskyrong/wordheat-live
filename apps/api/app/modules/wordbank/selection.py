"""One catalog lock, persistent per-session selection revisions, immutable history."""
import sqlalchemy as sa
from app.infrastructure.db import schema as s
from app.modules.auth.service import uid
from app.errors import DomainError


def catalog_lock(c):
    # Called at the outermost transaction before session/round/puzzle locks.
    return c.execute(sa.select(s.catalog).where(s.catalog.c.id=='global').with_for_update()).mappings().one()


def pointer(c,sid):
    return c.execute(sa.select(s.selection_state).where(s.selection_state.c.session_id==sid)).mappings().one()


def rules_for(c,session):
    config=c.execute(sa.select(s.configuration).where(s.configuration.c.session_id==session['id'])).mappings().first()
    return config['next_rules'] if config and config['next_rules'] else session['rules']


def eligible(c,session,strategy,puzzle_id=None,exclude_current=True):
    query=sa.select(s.puzzles).where(s.puzzles.c.status=='published',s.puzzles.c.manifest_id.is_not(None),s.puzzles.c.calibration_id.is_not(None),s.puzzles.c.evaluation_id.is_not(None),s.puzzles.c.approved_by.is_not(None))
    latest=sa.select(s.puzzles.c.puzzle_id,sa.func.max(s.puzzles.c.version).label('latest')).where(s.puzzles.c.status=='published').group_by(s.puzzles.c.puzzle_id).subquery()
    query=query.join(latest,sa.and_(latest.c.puzzle_id==s.puzzles.c.puzzle_id,latest.c.latest==s.puzzles.c.version))
    if '_manifest_id' in session: query=query.where(s.puzzles.c.manifest_id==session['_manifest_id'])
    if session.get('_manifest_sha256'): query=query.where(s.puzzles.c.scoring_artifact['manifest_sha256'].astext==session['_manifest_sha256'])
    catalog=c.execute(sa.select(s.catalog)).mappings().one()
    joint=catalog['joint_identity']
    if joint:
        query=query.where(s.puzzles.c.id.in_([p['puzzle_version_id'] for p in joint['puzzles']]))
    rules=rules_for(c,session); query=query.where(s.puzzles.c.difficulty.between(rules['difficulty_min'],rules['difficulty_max']))
    if rules.get('category_id'): query=query.where(s.puzzles.c.category_id==rules['category_id'])
    if exclude_current:
        active=sa.select(s.puzzles.c.puzzle_id).join(s.rounds,s.rounds.c.puzzle_version_id==s.puzzles.c.id).where(s.rounds.c.session_id==session['id'],s.rounds.c.status.in_(['PREPARING','RUNNING','PAUSED','DRAINING','FAULTED']))
        query=query.where(s.puzzles.c.puzzle_id.not_in(active))
    if strategy=='specific':
        if not puzzle_id: raise DomainError('INPUT_INVALID','puzzle_id required',422)
        if not c.execute(sa.select(s.puzzles.c.id).where(s.puzzles.c.puzzle_id==puzzle_id)).first(): raise DomainError('RESOURCE_NOT_FOUND','Puzzle not found',404)
        query=query.where(s.puzzles.c.puzzle_id==puzzle_id)
    return c.execute(query.order_by(sa.func.random() if strategy=='random' else s.puzzles.c.version.desc()).limit(1)).mappings().first()


def replace(c,session,strategy,puzzle_id=None,reason='REPLACED'):
    current=pointer(c,session['id']); rev=current['revision']; catalog=c.execute(sa.select(s.catalog)).mappings().one()
    if current['selection_id']:
        old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==current['selection_id'])).mappings().one()
        if old['status']=='READY':
            rev+=1
            c.execute(s.next_selections.update().where(s.next_selections.c.id==old['id']).values(status='INVALIDATED',reason_code=reason,invalidated_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)))
    puzzle=eligible(c,session,strategy,puzzle_id); rev+=1; selection_id=uid()
    c.execute(s.next_selections.insert().values(id=selection_id,session_id=session['id'],version=rev,status='READY' if puzzle else 'EMPTY',puzzle_version_id=puzzle['id'] if puzzle else None,strategy=strategy,manifest_id=puzzle['manifest_id'] if puzzle else None,calibration_id=puzzle['calibration_id'] if puzzle else None,next_rules_version=c.execute(sa.select(s.configuration.c.next_rules_version).where(s.configuration.c.session_id==session['id'])).scalar_one(),catalog_revision=catalog['revision'],reason_code=None if puzzle else 'NO_ELIGIBLE_PUZZLE'))
    c.execute(s.selection_state.update().where(s.selection_state.c.session_id==session['id']).values(revision=rev,selection_id=selection_id))
    c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(version=s.sessions.c.version+(rev-current['revision'])))
    return {'selection_id':selection_id,'version':rev,'status':'READY' if puzzle else 'EMPTY'}


def consume(c,session,body):
    current=pointer(c,session['id']); old=None
    if current['selection_id']: old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==current['selection_id'])).mappings().one()
    # A saved reference always denotes that exact intent, even after it became
    # EMPTY/CONSUMED. It must never fall through to the first-selection path.
    if 'selection_id' in body or 'expected_selection_version' in body:
        if (not old or old['status']!='READY' or body.get('selection_id')!=old['id']
                or body.get('expected_selection_version')!=current['revision']):
            prior=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==body.get('selection_id'),s.next_selections.c.session_id==session['id'])).mappings().first()
            if prior and prior['status']=='INVALIDATED' and prior['reason_code'] in {'PUZZLE_DISABLED','NEXT_RULES_CHANGED','CATALOG_CHANGED','MANIFEST_CHANGED'}:
                raise DomainError('NEXT_SELECTION_STALE','Selected puzzle no longer eligible',409)
            raise DomainError('NEXT_SELECTION_VERSION_CONFLICT','Selection changed',409)
    if old and old['status']=='READY':
        if 'selection_id' not in body or 'expected_selection_version' not in body: raise DomainError('NEXT_SELECTION_REQUIRED','Persistent selection reference required',409)
        if body['selection_id']!=old['id'] or body['expected_selection_version']!=current['revision']: raise DomainError('NEXT_SELECTION_VERSION_CONFLICT','Selection changed',409)
        puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==old['puzzle_version_id'],s.puzzles.c.status=='published')).mappings().first()
        if not puzzle: raise DomainError('NEXT_SELECTION_STALE','Selected puzzle no longer eligible',409)
        rules=rules_for(c,session)
        if (not rules['difficulty_min']<=puzzle['difficulty']<=rules['difficulty_max'] or (rules.get('category_id') and puzzle['category_id']!=rules['category_id']) or ('_manifest_id' in session and puzzle['manifest_id']!=session['_manifest_id']) or not all(puzzle[key] for key in ('manifest_id','calibration_id','evaluation_id','approved_by'))): raise DomainError('NEXT_SELECTION_STALE','Selected puzzle no longer matches next rules',409)
        if session.get('_manifest_sha256') and puzzle['scoring_artifact'].get('manifest_sha256')!=session['_manifest_sha256']: raise DomainError('NEXT_SELECTION_STALE','Frozen manifest checksum changed',409)
        requested=body.get('selection')
        if requested and (requested['strategy']!=old['strategy'] or (requested.get('puzzle_id') and requested['puzzle_id']!=puzzle['puzzle_id'])): raise DomainError('NEXT_SELECTION_MISMATCH','Selection request differs from preview',409)
    else:
        requested=body.get('selection')
        if not requested: raise DomainError('NEXT_SELECTION_REQUIRED','Explicit first selection required',409)
        result=replace(c,session,requested['strategy'],requested.get('puzzle_id'))
        if result['status']!='READY': raise DomainError('NO_ELIGIBLE_PUZZLE','No eligible puzzle available',409)
        current=pointer(c,session['id']); old=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==current['selection_id'])).mappings().one()
        puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==old['puzzle_version_id'])).mappings().one()
    rev=current['revision']+1
    c.execute(s.next_selections.update().where(s.next_selections.c.id==old['id']).values(status='CONSUMED'))
    c.execute(s.selection_state.update().where(s.selection_state.c.session_id==session['id']).values(revision=rev))
    c.execute(s.sessions.update().where(s.sessions.c.id==session['id']).values(version=s.sessions.c.version+1))
    return dict(puzzle),old['id'],rev


def projection(c,sid):
    state=pointer(c,sid); record=c.execute(sa.select(s.next_selections).where(s.next_selections.c.id==state['selection_id'])).mappings().first() if state['selection_id'] else None
    puzzle=c.execute(sa.select(s.puzzles).where(s.puzzles.c.id==record['puzzle_version_id'])).mappings().first() if record and record['status']=='READY' else None
    return {'selection_id':record['id'] if record else None,'version':state['revision'],'status':record['status'] if record and record['status']!='CONSUMED' else 'NONE','puzzle_version_id':puzzle['id'] if puzzle else None,'category_id':puzzle['category_id'] if puzzle else None,'difficulty':puzzle['difficulty'] if puzzle else None,'answer':puzzle['word'] if puzzle else None,'aliases':puzzle['aliases'] if puzzle else [],'published_hints':puzzle['hints'] if puzzle else [],'explanation':puzzle['explanation'] if puzzle else None,'next_rules_version':record['next_rules_version'] if record else c.execute(sa.select(s.configuration.c.next_rules_version).where(s.configuration.c.session_id==sid)).scalar_one(),'manifest_id':puzzle['manifest_id'] if puzzle else None,'calibration_id':puzzle['calibration_id'] if puzzle else None,'selected_at':record['created_at'].isoformat(timespec='milliseconds').replace('+00:00','Z') if record else None,'reason_code':record['reason_code'] if record else None}

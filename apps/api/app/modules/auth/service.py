import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
import sqlalchemy as sa
from app.errors import DomainError
from app.infrastructure.db import schema as s

PERMISSIONS = ['session.control','puzzle.edit','puzzle.review','diagnostics.read','replay.control','scoring.evaluate','maintenance.request','display.manage']

def now(): return datetime.now(timezone.utc)
def uid(): return secrets.token_urlsafe(18)
def token_hash(value): return hashlib.sha256(value.encode()).hexdigest()

def hash_password(password):
    if len(password) < 12 or len(password) > 256:
        raise DomainError('INPUT_INVALID', 'Password must be 12–256 characters', 422)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return 'scrypt$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(digest).decode()

def verify_password(password, encoded):
    try:
        _, salt, digest = encoded.split('$')
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def bootstrap_operator(connection, username, password, display_name):
    if not username or len(username)>80 or not display_name or len(display_name)>80:
        raise DomainError('INPUT_INVALID', 'Invalid account fields', 422)
    connection.execute(sa.text('SELECT pg_advisory_xact_lock(674219385)'))
    if connection.execute(sa.select(sa.func.count()).select_from(s.operators)).scalar_one():
        raise DomainError('BOOTSTRAP_CLOSED', 'Account initialization already completed', 409)
    actor_id = uid()
    connection.execute(s.operators.insert().values(id=actor_id, username=username, password_hash=hash_password(password), display_name=display_name, permissions=PERMISSIONS))
    return actor_id


def set_operator_state(connection, username, disabled):
    """Controlled maintenance state change; deliberately not a Web operation.

    Disabling stamps disabled_at and revokes every live session of that actor, so
    a disabled account cannot keep acting until its sessions expire.
    """
    if not username or len(username)>80: raise DomainError('INPUT_INVALID','Invalid account fields',422)
    connection.execute(sa.text('SELECT pg_advisory_xact_lock(674219385)'))
    row = connection.execute(sa.select(s.operators).where(s.operators.c.username==username)).mappings().first()
    if not row: raise DomainError('RESOURCE_NOT_FOUND','Operator not found',404)
    if row['disabled'] is disabled: return row['id']
    connection.execute(s.operators.update().where(s.operators.c.id==row['id']).values(disabled=disabled, disabled_at=now() if disabled else None))
    if disabled:
        connection.execute(s.auth_sessions.update().where(s.auth_sessions.c.actor_id==row['id'], s.auth_sessions.c.revoked_at.is_(None)).values(revoked_at=now()))
    return row['id']


class AuthService:
    def __init__(self, database): self.db = database

    def login(self, username, password):
        with self.db.engine.begin() as c:
            actor = c.execute(sa.select(s.operators).where(s.operators.c.username==username)).mappings().first()
            if not actor or actor['disabled'] or not verify_password(password, actor['password_hash']):
                raise DomainError('AUTH_REQUIRED', 'Invalid credentials', 401)
            token, csrf = uid(), uid()
            expires=now()+timedelta(hours=8)
            c.execute(s.auth_sessions.insert().values(id=uid(), actor_id=actor['id'], token_hash=token_hash(token), csrf=csrf, expires_at=expires))
            return token, self.actor_dto(actor, csrf, expires)

    @staticmethod
    def actor_dto(actor, csrf, expires):
        return {'actor':{'actor_id':actor['id'], 'display_name':actor['display_name'], 'permissions':actor['permissions']}, 'csrf_token':csrf,'expires_at':expires.isoformat(timespec='milliseconds').replace('+00:00','Z')}

    def operator(self, token, permission=None, csrf=None):
        if not token: raise DomainError('AUTH_REQUIRED','Operator login required',401)
        with self.db.engine.connect() as c:
            row = c.execute(sa.select(s.operators, s.auth_sessions.c.csrf, s.auth_sessions.c.expires_at).join(s.auth_sessions, s.auth_sessions.c.actor_id==s.operators.c.id).where(s.auth_sessions.c.token_hash==token_hash(token), s.auth_sessions.c.revoked_at.is_(None), s.auth_sessions.c.expires_at>now(), s.operators.c.disabled.is_(False))).mappings().first()
            if not row: raise DomainError('AUTH_REQUIRED','Operator session expired',401)
            if permission and permission not in row['permissions']: raise DomainError('FORBIDDEN','Permission required',403)
            if csrf is not None and not hmac.compare_digest(csrf, row['csrf']): raise DomainError('FORBIDDEN','CSRF validation failed',403)
            return {'actor_id':row['id'],'display_name':row['display_name'],'permissions':row['permissions'],'csrf_token':row['csrf'],'expires_at':row['expires_at'].isoformat(timespec='milliseconds').replace('+00:00','Z')}

    def logout(self, token):
        # Revocation keeps the audit row: a logout is a revocation of one session.
        with self.db.engine.begin() as c:
            c.execute(s.auth_sessions.update().where(s.auth_sessions.c.token_hash==token_hash(token), s.auth_sessions.c.revoked_at.is_(None)).values(revoked_at=now()))
            c.execute(s.auth_sessions.delete().where(s.auth_sessions.c.expires_at<now()-timedelta(hours=24)))

    def sessions(self, actor, token):
        # Ids are the only handle an operator needs to revoke a session; no token
        # material is ever returned. `current` marks the calling session itself.
        current_hash = token_hash(token) if token else None
        with self.db.engine.connect() as c:
            rows = list(c.execute(sa.select(s.auth_sessions).where(s.auth_sessions.c.actor_id==actor['actor_id'], s.auth_sessions.c.expires_at>now(), s.auth_sessions.c.revoked_at.is_(None)).order_by(s.auth_sessions.c.created_at)).mappings())
        return [{'session_id':row['id'],'actor_id':row['actor_id'],'created_at':row['created_at'].isoformat(timespec='milliseconds').replace('+00:00','Z'),'expires_at':row['expires_at'].isoformat(timespec='milliseconds').replace('+00:00','Z'),'current':row['token_hash']==current_hash} for row in rows]

    def revoke(self, actor, session_id):
        with self.db.engine.begin() as c:
            row = c.execute(sa.select(s.auth_sessions).where(s.auth_sessions.c.id==session_id)).mappings().first()
            if not row: raise DomainError('RESOURCE_NOT_FOUND','Operator session not found',404)
            if row['actor_id']!=actor['actor_id'] and 'maintenance.request' not in actor['permissions']:
                raise DomainError('FORBIDDEN','Maintenance permission required to revoke another operator session',403)
            if row['revoked_at'] is not None: return {'revoked':True}
            c.execute(s.auth_sessions.update().where(s.auth_sessions.c.id==session_id).values(revoked_at=now(), revoked_by=actor['actor_id']))
            return {'revoked':True}

    def display(self, token):
        if not token: raise DomainError('DISPLAY_UNAUTHORIZED','Display login required',403)
        with self.db.engine.connect() as c:
            row = c.execute(sa.select(s.displays).where(s.displays.c.token_hash==token_hash(token), s.displays.c.expires_at>now(), s.displays.c.revoked.is_(False))).mappings().first()
            if not row: raise DomainError('DISPLAY_UNAUTHORIZED','Display session expired',403)
            return dict(row)

    @staticmethod
    def display_dto(row):
        return {'display_instance_id':row['id'],'render_generation':str(row['render_generation']),'session_id':row['session_id'],'purpose':row['purpose'],'receipt_csrf_token':row['csrf'],'expires_at':row['expires_at'].isoformat(timespec='milliseconds').replace('+00:00','Z')}

"""Closed internal management scope, separate from live-grant permissions."""
from datetime import datetime,timedelta,timezone
import re


def validate(payload,now=None):
    required={'object_id','issuer_id','issuer_revision','actor_id','app_id','room_id','room_binding_id','installation_id','candidate_digest','device_key_sha256','anchor_open_id_sha256','not_before','expires_at','allowed_actions'}
    if not isinstance(payload,dict) or set(payload)!=required:raise ValueError('Invalid pairing intent')
    if any(not isinstance(payload[k],str) or not 0<len(payload[k])<=256 for k in ('object_id','issuer_id','actor_id','app_id','room_id','room_binding_id','installation_id')):raise ValueError('Invalid pairing identity')
    if type(payload['issuer_revision']) is not int or payload['issuer_revision']<1 or payload['allowed_actions']!=['pair_device']:raise ValueError('Invalid management permission')
    if any(not isinstance(payload[k],str) or not re.fullmatch('[0-9a-f]{64}',payload[k]) for k in ('candidate_digest','device_key_sha256','anchor_open_id_sha256')):raise ValueError('Invalid pairing binding')
    try:
        start=datetime.fromisoformat(payload['not_before'].replace('Z','+00:00'));end=datetime.fromisoformat(payload['expires_at'].replace('Z','+00:00'))
        if start.tzinfo is None or end.tzinfo is None or end<=start or end-start>timedelta(minutes=15):raise ValueError()
        if now is not None and not start<=now<end:raise ValueError()
    except (TypeError,AttributeError,ValueError):raise ValueError('Pairing intent outside its validity') from None
    return payload

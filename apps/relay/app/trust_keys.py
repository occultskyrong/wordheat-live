"""Root-signed internal key bundles; algorithms remain in mature libraries."""
import base64
from dataclasses import dataclass
import hashlib
import json
import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

TRUST_DOMAIN='wordheat-live.trust'
READINESS_DOMAIN='wordheat-live.readiness'
READINESS_TYPES={'gate_evidence','live_grant','revocation','freshness_proof','pairing_intent'}


class TrustKeyError(ValueError):pass


@dataclass(frozen=True)
class ImportedKeyBundle:
    object_id:str
    digest:str
    issuer_revision:int
    key_ids:tuple[str,...]
    revoked_key_ids:tuple[str,...]
    root_chain:tuple[str,...]
    imported:bool=True


def reject():raise TrustKeyError('Invalid root-signed key bundle')


def raw32(value,size=32):
    try:
        if not isinstance(value,str):reject()
        result=base64.urlsafe_b64decode(value+'='*((4-len(value)%4)%4))
        if len(result)!=size or base64.urlsafe_b64encode(result).rstrip(b'=').decode()!=value:reject()
        return result
    except (ValueError,TypeError):reject()


def parse_bundle(raw):
    def pairs(items):
        obj={}
        for key,value in items:
            if key in obj:reject()
            obj[key]=value
        return obj
    try:
        if isinstance(raw,(str,bytes)):
            if len(raw)>1048576:reject()
            obj=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:reject())
        else:obj=raw
        encoded=rfc8785.dumps(obj)
        if len(encoded)>1048576 or not isinstance(obj,dict):reject()
        return obj
    except (ValueError,TypeError,UnicodeError,RecursionError):reject()


def initial_state(roots):
    keys={};lineage={}
    for key_id,value in roots.items():
        if not isinstance(key_id,str) or not key_id or not isinstance(value,dict) or not isinstance(value.get('public_key'),bytes) or len(value['public_key'])!=32:reject()
        keys[key_id]=dict(value)|{'revoked':False};lineage[key_id]=(key_id,)
    return keys,lineage,{},{}


def apply_bundle(raw,keys,lineage,objects,issuer_revisions):
    obj=parse_bundle(raw)
    if set(obj)!={'key_id','domain','type','version','payload','signature'} or obj['domain']!=TRUST_DOMAIN or obj['type']!='key_bundle' or type(obj['version']) is not int or obj['version']!=1:reject()
    p=obj['payload']
    if not isinstance(p,dict) or set(p)!={'object_id','issuer_id','issuer_revision','keys','revoked_key_ids'}:reject()
    if any(not isinstance(p.get(k),str) or not 0<len(p[k])<=128 for k in ('object_id','issuer_id')) or type(p['issuer_revision']) is not int or p['issuer_revision']<1:reject()
    unsigned={k:v for k,v in obj.items() if k!='signature'};canonical=rfc8785.dumps(unsigned);digest=hashlib.sha256(canonical).hexdigest()
    if p['object_id'] in objects:
        old=objects[p['object_id']]
        if old.digest!=digest:reject()
        return ImportedKeyBundle(old.object_id,old.digest,old.issuer_revision,old.key_ids,old.revoked_key_ids,old.root_chain,False)
    signer=keys.get(obj['key_id'])
    if not signer or signer.get('revoked') or TRUST_DOMAIN not in signer.get('domains',()) or 'key_bundle' not in signer.get('types',()) or obj['key_id'] not in lineage:reject()
    try:Ed25519PublicKey.from_public_bytes(signer['public_key']).verify(raw32(obj['signature'],64),canonical)
    except (InvalidSignature,ValueError):reject()
    if p['issuer_revision']<=issuer_revisions.get(p['issuer_id'],0):reject()
    if not isinstance(p['keys'],list) or len(p['keys'])>128 or not isinstance(p['revoked_key_ids'],list) or len(p['revoked_key_ids'])>128:reject()
    additions={}
    for entry in p['keys']:
        if not isinstance(entry,dict) or set(entry)!={'key_id','public_key_b64url','domains','types'}:reject()
        key_id=entry['key_id'];domains=entry['domains'];types=entry['types']
        if not isinstance(key_id,str) or not 0<len(key_id)<=128 or key_id in additions or not isinstance(domains,list) or not isinstance(types,list) or not domains or not types or any(not isinstance(v,str) for v in domains+types) or len(domains)!=len(set(domains)) or len(types)!=len(set(types)):reject()
        if not set(domains)<={TRUST_DOMAIN,READINESS_DOMAIN} or not set(types)<=READINESS_TYPES|{'key_bundle'}:reject()
        if ('key_bundle' in types)!=(TRUST_DOMAIN in domains):reject()
        if READINESS_TYPES.intersection(types) and READINESS_DOMAIN not in domains:reject()
        public=raw32(entry['public_key_b64url']);old=keys.get(key_id)
        if old and (old.get('revoked') or old['public_key']!=public or set(old.get('domains',()))!=set(domains) or set(old.get('types',()))!=set(types)):reject()
        additions[key_id]={'public_key':public,'domains':tuple(domains),'types':tuple(types),'revoked':False,'test_only':bool(signer.get('test_only'))}
    revoked=p['revoked_key_ids']
    if any(not isinstance(k,str) or k not in keys and k not in additions for k in revoked) or len(revoked)!=len(set(revoked)):reject()
    for key_id,value in additions.items():
        keys[key_id]=value
        lineage.setdefault(key_id,lineage[obj['key_id']]+(key_id,))
    for key_id in revoked:keys[key_id]=dict(keys[key_id])|{'revoked':True}
    issuer_revisions[p['issuer_id']]=p['issuer_revision']
    result=ImportedKeyBundle(p['object_id'],digest,p['issuer_revision'],tuple(additions),tuple(revoked),lineage[obj['key_id']])
    objects[p['object_id']]=result
    return result


def historical_keys(keys):return {name:dict(value)|{'revoked':False} for name,value in keys.items()}

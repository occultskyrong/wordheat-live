"""Explicit test-only engine namespace capability. Never creates or drops schemas.

The parent fixture owns the schema. Every checkout revalidates its database,
owner, random comment, installation marker and runtime no-CREATE privilege before
setting a search_path containing only that schema (no public fallback).
"""
from dataclasses import dataclass
import json,re
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from psycopg import sql
from app.config import read_private_bytes
from app.errors import DomainError

@dataclass(frozen=True)
class TestNamespace:
    database:str
    database_marker:str
    installation_id:str
    namespace:str
    owner:str
    comment:str

def fail():raise DomainError('TEST_NAMESPACE_REJECTED','Registered isolated test namespace could not be verified',503)

def load(settings):
    if getattr(settings,'environment',None)!='test':fail()
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:fail()
            value[key]=item
        return value
    try:
        value=json.loads(read_private_bytes(settings.test_namespace_file,16384),object_pairs_hook=unique,parse_constant=lambda _:fail())
        if set(value)!={'version','database','database_marker','installation_id','namespace','owner','comment'} or type(value['version']) is not int or value['version']!=1 or value['database']!='wordheat_test' or value['database_marker']!='wordheat-live:test:local' or value['installation_id']!=settings.instance_id or make_url(settings.database_url).database!='wordheat_test':fail()
        if not isinstance(value['namespace'],str) or not re.fullmatch('wordheat_pytest_[0-9a-f]{32}',value['namespace']) or not isinstance(value['owner'],str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,62}',value['owner']) or not isinstance(value['comment'],str) or not re.fullmatch('wordheat-live:pytest:[0-9a-f]{64}',value['comment']):fail()
        return TestNamespace(**{k:v for k,v in value.items() if k!='version'})
    except DomainError:raise
    except Exception:fail()

def install(engine,settings):
    descriptor=load(settings)
    def checkout(raw,record,proxy):
        previous=raw.autocommit
        try:
            raw.autocommit=True
            with raw.cursor() as cursor:
                cursor.execute("SELECT current_database(),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()")
                if cursor.fetchone()!=(descriptor.database,descriptor.database_marker):fail()
                cursor.execute('SELECT instance_id FROM public.service_instance')
                if cursor.fetchone()!=(descriptor.installation_id,):fail()
                cursor.execute("SELECT pg_get_userbyid(nspowner),obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=%s",(descriptor.namespace,))
                if cursor.fetchone()!=(descriptor.owner,descriptor.comment):fail()
                cursor.execute("SELECT has_schema_privilege(current_user,%s,'CREATE')",(descriptor.namespace,))
                if cursor.fetchone()!=(False,):fail()
                cursor.execute(sql.SQL('SELECT instance_id FROM {}.service_instance').format(sql.Identifier(descriptor.namespace)))
                if cursor.fetchone()!=(descriptor.installation_id,):fail()
                cursor.execute("SELECT set_config('search_path',%s,false)",(descriptor.namespace,))
        except DomainError:raise
        except Exception:fail()
        finally:raw.autocommit=previous
    sa.event.listen(engine,'checkout',checkout)
    return descriptor

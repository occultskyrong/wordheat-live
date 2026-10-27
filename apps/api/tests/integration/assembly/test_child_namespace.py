import sys,json,tempfile
from pathlib import Path
import pytest
import sqlalchemy as sa
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_local_flow import environment
from app.config import Settings
from app.infrastructure.db.database import Database
from app.errors import DomainError

def registration(root,isolation,instance_id):
    owner,marker=isolation.owned[isolation.schema]
    value={'version':1,'database':'wordheat_test','database_marker':'wordheat-live:test:local','installation_id':instance_id,'namespace':isolation.schema,'owner':owner,'comment':marker}
    path=root/'test-namespace.json';path.write_text(json.dumps(value));path.chmod(0o600);return path,value

def test_child_namespace_is_explicit_and_engine_scoped(environment,postgres_isolation):
    db,actor=environment
    with tempfile.TemporaryDirectory(prefix='wh-namespace-',dir='/private/tmp') as directory:
        path,value=registration(Path(directory),postgres_isolation,db.settings.instance_id)
        settings=Settings(db.settings.database_url,instance_id=db.settings.instance_id,environment='test',test_namespace_file=path)
        scoped=Database(settings)
        try:
            assert scoped.test_namespace.namespace==value['namespace']
            with scoped.engine.connect() as c:
                assert c.execute(sa.text('SELECT current_schema()')).scalar_one()==value['namespace']
                assert c.execute(sa.text("SELECT current_setting('search_path')")).scalar_one()==value['namespace']
                assert c.execute(sa.text("SELECT has_schema_privilege(current_user,current_schema(),'CREATE')")).scalar_one() is False
        finally:scoped.engine.dispose()

def test_namespace_registration_cannot_enable_development_or_another_owner(environment,postgres_isolation):
    db,actor=environment
    with tempfile.TemporaryDirectory(prefix='wh-namespace-',dir='/private/tmp') as directory:
        path,value=registration(Path(directory),postgres_isolation,db.settings.instance_id)
        with pytest.raises(ValueError):Settings(db.settings.database_url,environment='development',test_namespace_file=path)
        value['owner']='unregistered_owner';path.write_text(json.dumps(value));path.chmod(0o600)
        settings=Settings(db.settings.database_url,instance_id=db.settings.instance_id,environment='test',test_namespace_file=path)
        scoped=Database(settings)
        try:
            with pytest.raises(DomainError):scoped.verify_runtime()
        finally:scoped.engine.dispose()

import pytest
from app.errors import DomainError
from app.infrastructure.db import verification

class Result:
    def __init__(self,value): self.value=value
    def mappings(self): return self
    def one(self): return self.value
    def all(self): return self.value
class Connection:
    def __init__(self,value): self.value=value
    def execute(self,*args): return Result(self.value)

def test_connected_identity_rejects_wrong_pg_or_database_marker():
    value={'name':'wordheat_test','version':160014,'marker':'wordheat-live:test:local'}
    verification.verify_target(Connection(value))
    for change in ({'version':170001},{'marker':None},{'name':'other_project'}):
        with pytest.raises(DomainError): verification.verify_target(Connection(value|change))

def test_runtime_requires_every_packaged_checksum(monkeypatch):
    monkeypatch.setattr(verification,'packaged_revisions',lambda:{'initial.sql':'a','wordbank.sql':'b'})
    verification.verify_revisions(Connection([('initial.sql','a'),('wordbank.sql','b')]))
    for rows in ([('initial.sql','a')],[('initial.sql','a'),('wordbank.sql','changed')]):
        with pytest.raises(DomainError): verification.verify_revisions(Connection(rows))

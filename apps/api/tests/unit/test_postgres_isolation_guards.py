"""Pure guards do not require configuration or a PostgreSQL connection."""
import pytest
from sqlalchemy.engine import make_url
from conftest import PostgresIsolation


def boundary():
    isolation = PostgresIsolation.__new__(PostgresIsolation)
    isolation.runtime = make_url("postgresql+psycopg://wordheat_app:synthetic@127.0.0.1/wordheat_test")
    isolation.migration = make_url("postgresql+psycopg://wordheat_migrator:synthetic@127.0.0.1/wordheat_test")
    isolation.owned = {}
    return isolation


def test_only_exact_test_dsns_match():
    isolation = boundary()
    assert isolation.matches(isolation.runtime)
    assert isolation.matches(isolation.migration)
    assert isolation.matches(isolation.runtime.set(drivername="postgresql"))
    for changed in [
        isolation.runtime.set(database="wordheat_other"),
        isolation.runtime.set(host="localhost"),
        isolation.runtime.set(username="wordheat_other"),
        isolation.runtime.set(password="other-synthetic"),
        isolation.runtime.update_query_dict({"options": "-csearch_path=public"}),
        isolation.runtime.set(drivername="sqlite"),
        "not-a-dsn",
    ]:
        assert not isolation.matches(changed), "Only the exact configured test target may be intercepted"


def test_cleanup_refuses_unowned_schema_before_connecting():
    isolation = boundary()
    for name in ["public", "pg_catalog", "wordheat_pytest_unowned"]:
        with pytest.raises(ValueError, match="unowned"):
            isolation.drop_schema(name)

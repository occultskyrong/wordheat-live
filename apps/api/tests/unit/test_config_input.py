import importlib
import pytest


def test_reject_foreign_database_without_exposing_secret():
    config = importlib.import_module('app.config')
    with pytest.raises(ValueError) as error:
        config.Settings(database_url='postgresql://user:sentinel-secret@127.0.0.1/other_project')
    assert 'sentinel-secret' not in str(error.value)


def test_han_normalization_is_explicit():
    normalize = importlib.import_module('app.modules.inbox.normalization').normalize_word
    assert normalize('  苹果  ') == '苹果'
    for value in ['a苹果', '苹', '苹果西瓜梨', '蘋果!']:
        with pytest.raises(Exception):
            normalize(value)


@pytest.mark.parametrize('query',['host=external.invalid&dbname=other_project','host=127.0.0.1&dbname=wordheat_prod','user=foreign','options=-csearch_path%3Dforeign','connect_timeout=1'])
def test_dsn_query_cannot_override_validated_target(query):
    from app.config import validate_dsn
    with pytest.raises(ValueError) as error:
        validate_dsn('postgresql://wordheat_test_app:sentinel-secret@127.0.0.1/wordheat_test?'+query)
    assert 'sentinel-secret' not in str(error.value)

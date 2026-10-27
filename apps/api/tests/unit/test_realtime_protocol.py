import importlib
import pytest


def test_resume_is_strict_and_decimal_cursor_never_float():
    module=importlib.import_module('app.modules.realtime.protocol')
    base={'kind':'resume','schema_version':1,'contract_version':'LW-C1/1.0.0','session_id':'s','after_seq':'0'}
    assert module.parse_resume(base)['after_seq']=='0'
    for value in [1,'-1','01','1.5']:
        with pytest.raises(Exception): module.parse_resume(base|{'after_seq':value})
    with pytest.raises(Exception): module.parse_resume(base|{'audience':'STUDIO'})


def test_receipt_limits_and_processing_evidence_shape():
    module=importlib.import_module('app.modules.realtime.protocol')
    receipt={'display_instance_id':'d','render_generation':'1','batch_id':'batch','stage':'processed','deliveries':[{'delivery_id':'x','client_time':'2026-10-25T01:00:00.000Z','feedback_id':'f'}]}
    assert module.parse_receipt(receipt)['stage']=='processed'
    with pytest.raises(Exception): module.parse_receipt(receipt|{'render_generation':1})
    with pytest.raises(Exception): module.parse_receipt(receipt|{'deliveries':receipt['deliveries']*129})

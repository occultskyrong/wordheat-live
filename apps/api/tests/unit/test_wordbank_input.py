import pytest
from pydantic import ValidationError

def test_draft_rejects_server_quality_fields_and_non_han():
    from app.modules.wordbank.models import Draft
    base=dict(word='苹果',aliases=[],category_id='fruit',difficulty=1,hints=[],explanation='水果',source_ref='owned')
    assert Draft(**base).word=='苹果'
    with pytest.raises(ValidationError): Draft(**(base|{'passed':True}))
    with pytest.raises(ValidationError): Draft(**(base|{'word':'apple'}))

def test_csv_report_is_bounded_utf8_bom_and_row_specific():
    from app.modules.wordbank.service import parse_csv
    rows,errors=parse_csv(('\ufeffword,aliases,category_id,difficulty,hints,explanation,source_ref\n苹果,[],fruit,1,[],水果,owned\nbanana,[],fruit,1,[],水果,owned\n').encode())
    assert len(rows)==1 and rows[0]['row']==2 and errors[0]['row']==3
    from app.errors import DomainError
    with pytest.raises(DomainError): parse_csv(b'x'*(2*1024*1024+1))

def test_calibration_is_fitted_and_overlapping_labels_rejected():
    from app.modules.wordbank.evaluation import fit_calibration
    result=fit_calibration([(.1,'cold'),(.2,'cold'),(.6,'warm'),(.85,'hot'),(.95,'hot')])
    assert result['u']>result['b'] and result['gamma']>0
    assert result['b']==.2 and result['u']==.85
    from app.errors import DomainError
    with pytest.raises(DomainError): fit_calibration([(.9,'cold'),(.2,'hot'),(.5,'warm')])

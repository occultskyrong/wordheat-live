"""Strict browser WS/receipt input shapes. Authentication never comes from JSON."""
import json
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator
from app.errors import DomainError

class Strict(BaseModel): model_config=ConfigDict(extra='forbid',strict=True)
class Resume(Strict):
    kind:Literal['resume']; schema_version:Literal[1]; contract_version:Literal['LW-C1/1.0.0']
    session_id:str=Field(min_length=1,max_length=128); after_seq:str=Field(pattern=r'^(0|[1-9][0-9]*)$',max_length=30)
    be_extension_version:str|None=None
class Delivery(Strict):
    delivery_id:str=Field(min_length=1,max_length=128); client_time:str=Field(max_length=40); feedback_id:str=Field(min_length=1,max_length=128)
    @field_validator('client_time')
    @classmethod
    def timestamp(cls,value):
        from app.modules.auth.evidence import parse_time
        parse_time(value); return value
class Receipt(Strict):
    display_instance_id:str; render_generation:str=Field(pattern=r'^(0|[1-9][0-9]*)$',max_length=30)
    batch_id:str=Field(min_length=1,max_length=128); stage:Literal['received','processed']; deliveries:list[Delivery]=Field(min_length=1,max_length=128)

def parse_resume(value): return Resume.model_validate(value).model_dump(exclude_none=True)
def parse_receipt(value):
    result=Receipt.model_validate(value).model_dump()
    if len({item['delivery_id'] for item in result['deliveries']})!=len(result['deliveries']): raise DomainError('INPUT_INVALID','Duplicate delivery in receipt',422)
    return result

def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result: raise ValueError('duplicate key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError('nonfinite')))

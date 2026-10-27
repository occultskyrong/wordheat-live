from pydantic import Field,model_validator
from app.modules.wordbank.models import Input

class ReplayEvent(Input):
    source_id:str=Field(min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')
    offset_ms:int=Field(ge=0,le=86400000,strict=True)
    guess:str=Field(min_length=1,max_length=32)
    participant_key:str=Field(default='single-participant',min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')
    display_name:str=Field(default='Replay participant',min_length=1,max_length=80)

class ReplayCreate(Input):
    label:str=Field(min_length=1,max_length=80)
    events:list[ReplayEvent]=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def ordered(self):
        if len({event.source_id for event in self.events})!=len(self.events): raise ValueError('Duplicate source ID')
        if any(a.offset_ms>b.offset_ms for a,b in zip(self.events,self.events[1:])): raise ValueError('Offsets must be ordered')
        return self

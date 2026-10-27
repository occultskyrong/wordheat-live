"""Trusted relay input port; never deserialized by a browser HTTP route."""
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class OfficialAdmission:
    participant_key:str
    display_name:str
    stream_seq:int
    window_epoch:int
    received_at:datetime
    source_mode:str
    provider_test:bool

@dataclass(frozen=True)
class RehearsalAdmission:
    _token:object
    participant_ref:str
    display_name:str

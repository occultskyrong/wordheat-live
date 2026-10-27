"""Trusted application port; never a browser request model."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ReplayAdmission:
    job_id:str
    ordinal:int
    virtual_ms:int

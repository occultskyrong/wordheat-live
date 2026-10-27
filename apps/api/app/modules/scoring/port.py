from typing import Protocol
from app.errors import DomainError

class Scorer(Protocol):
    def score(self, *, word: str, frozen_puzzle: dict) -> float: ...

class UnavailableScorer:
    def score(self, **kwargs):
        raise DomainError('SCORER_NOT_READY','Real scorer is not configured',503,True)

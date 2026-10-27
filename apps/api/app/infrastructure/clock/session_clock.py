"""Local round admission clock; wall time never participates in adjudication."""
import time
from collections.abc import Callable
from app.errors import DomainError


def _budget(value):
    if type(value) is not int or value < 0:
        raise DomainError('INPUT_INVALID', 'remaining_ms must be a nonnegative integer', 422)
    return value


class LocalRoundClock:
    def __init__(self, boot_id: str, *, now_ns: Callable[[], int] = time.monotonic_ns):
        if not isinstance(boot_id, str) or not boot_id:
            raise DomainError('INPUT_INVALID', 'boot_id required', 422)
        self.boot_id = boot_id
        self._now_ns = now_ns
        self.state = 'IDLE'
        self.mono_started_ns = None
        self._remaining_ns = 0
        self._last_ns = None

    def _now(self):
        value = self._now_ns()
        if type(value) is not int or value < 0 or (self._last_ns is not None and value < self._last_ns):
            raise DomainError('CLOCK_UNCERTAIN', 'monotonic clock regressed')
        self._last_ns = value
        return value

    def start(self, duration_ms: int):
        duration_ms = _budget(duration_ms)
        if self.state != 'IDLE':
            raise DomainError('ROUND_VERSION_CONFLICT', 'clock already started')
        self.mono_started_ns = self._now()
        self._remaining_ns = duration_ms * 1_000_000
        self.state = 'RUNNING'

    def _left_ns(self):
        if self.state == 'RUNNING':
            return max(0, self._remaining_ns - (self._now() - self.mono_started_ns))
        return self._remaining_ns if self.state == 'PAUSED' else 0

    def remaining_ms(self) -> int:
        # Floor deliberately: persisted checkpoints may shorten but never extend.
        return self._left_ns() // 1_000_000

    def expired(self) -> bool:
        return self._left_ns() <= 0

    def pause(self):
        if self.state != 'RUNNING':
            raise DomainError('ROUND_VERSION_CONFLICT', 'clock is not running')
        self._remaining_ns = self._left_ns()
        self.state = 'PAUSED'
        self.mono_started_ns = None
        return self.remaining_ms()

    def resume(self):
        if self.state != 'PAUSED':
            raise DomainError('ROUND_VERSION_CONFLICT', 'clock is not paused')
        self.mono_started_ns = self._now()
        self.state = 'RUNNING'

    def snapshot(self) -> dict:
        # Running anchor stores the original budget, not a double-subtracted value.
        return {'boot_id': self.boot_id, 'state': self.state,
                'mono_started_ns': self.mono_started_ns,
                'remaining_ms': self._remaining_ns // 1_000_000}

    @classmethod
    def restore(cls, snapshot: dict, boot_id: str, *, now_ns=time.monotonic_ns,
                explicit_resume=False, checkpoint: dict | None = None):
        clock = cls(boot_id, now_ns=now_ns)
        budget = _budget(snapshot.get('remaining_ms'))
        state = snapshot.get('state')
        if state not in ('RUNNING', 'PAUSED', 'INTERRUPTED', 'IDLE'):
            raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE')
        if snapshot.get('boot_id') == boot_id:
            clock.state = state
            clock._remaining_ns = budget * 1_000_000
            anchor = snapshot.get('mono_started_ns')
            if state == 'RUNNING':
                if type(anchor) is not int or anchor < 0 or anchor > clock._now():
                    raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE')
                clock.mono_started_ns = anchor
            return clock
        if not explicit_resume:
            clock.state = 'INTERRUPTED'
            return clock
        if state == 'PAUSED':
            remaining = budget
        elif isinstance(checkpoint, dict) and checkpoint.get('complete') is True:
            # Checkpoint provenance/completeness must be established by persistence
            # adapter before calling this kernel. UTC-derived checkpoints forbidden.
            remaining = _budget(checkpoint.get('remaining_ms'))
            if remaining > budget:
                raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE')
        else:
            raise DomainError('LOCAL_TIME_RECOVERY_UNSAFE')
        clock._remaining_ns = remaining * 1_000_000
        clock.state = 'PAUSED'
        return clock

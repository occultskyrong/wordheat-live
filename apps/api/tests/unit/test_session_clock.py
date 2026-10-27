import unittest
from app.errors import DomainError
from app.infrastructure.clock.session_clock import LocalRoundClock

class ClockTests(unittest.TestCase):
    def test_pause_resume_and_deadline(self):
        now = [1_000_000]
        clock = LocalRoundClock('a', now_ns=lambda: now[0])
        clock.start(1000)
        now[0] += 125_000_000
        self.assertEqual(clock.remaining_ms(), 875)
        clock.pause()
        now[0] += 999_000_000
        self.assertEqual(clock.remaining_ms(), 875)
        clock.resume()
        now[0] += 875_000_000
        self.assertTrue(clock.expired())
        self.assertEqual(clock.remaining_ms(), 0)
    def test_same_boot_restore_and_cross_boot_paused(self):
        now = [0]
        clock = LocalRoundClock('a', now_ns=lambda: now[0]); clock.start(1000)
        now[0] = 200_000_000
        restored = LocalRoundClock.restore(clock.snapshot(), 'a', now_ns=lambda: now[0])
        self.assertEqual(restored.remaining_ms(), 800)
        restored.pause()
        fresh = LocalRoundClock.restore(restored.snapshot(), 'b', now_ns=lambda: 0, explicit_resume=True)
        self.assertEqual(fresh.remaining_ms(), 800)
        self.assertEqual(fresh.state, 'PAUSED')
    def test_cross_boot_running_is_interrupted_and_unsafe_resume_rejected(self):
        clock = LocalRoundClock('a', now_ns=lambda: 0); clock.start(1000)
        self.assertEqual(LocalRoundClock.restore(clock.snapshot(), 'b').state, 'INTERRUPTED')
        with self.assertRaises(DomainError) as raised:
            LocalRoundClock.restore(clock.snapshot(), 'b', explicit_resume=True)
        self.assertEqual(raised.exception.code, 'LOCAL_TIME_RECOVERY_UNSAFE')
    def test_invalid_transitions_and_monotonic_regression(self):
        now = [100]
        clock = LocalRoundClock('a', now_ns=lambda: now[0])
        for method in (clock.pause, clock.resume):
            with self.assertRaises(DomainError): method()
        with self.assertRaises(DomainError): clock.start(-1)
        clock.start(1)
        with self.assertRaises(DomainError): clock.start(1)
        now[0] = 99
        with self.assertRaises(DomainError): clock.remaining_ms()
    def test_checkpoint_can_only_shorten_cross_boot_budget(self):
        clock = LocalRoundClock('a', now_ns=lambda: 0); clock.start(1000)
        checkpoint = {'complete': True, 'remaining_ms': 400}
        restored = LocalRoundClock.restore(clock.snapshot(), 'b', explicit_resume=True, checkpoint=checkpoint, now_ns=lambda: 10)
        self.assertEqual(restored.remaining_ms(), 400)
        with self.assertRaises(DomainError):
            LocalRoundClock.restore(clock.snapshot(), 'b', explicit_resume=True, checkpoint={'complete':True,'remaining_ms':1001})

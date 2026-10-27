from concurrent.futures import ThreadPoolExecutor
from app.modules.inbox.limiter import GlobalAdmissionLimiter


def test_default_is_five_per_second_with_five_burst():
    clock=[0.0];bucket=GlobalAdmissionLimiter(clock=lambda:clock[0])
    assert (bucket.rate,bucket.burst)==(5,5)
    assert sum(bucket.allow() for _ in range(100))==5
    clock[0]=.5;assert sum(bucket.allow() for _ in range(100))==2
    assert bucket.tokens==.5
    clock[0]=1.;assert sum(bucket.allow() for _ in range(100))==3
    assert bucket.tokens==0.
    clock[0]=2.;assert sum(bucket.allow() for _ in range(100))==5


def test_concurrent_default_burst_cannot_admit_more_than_five():
    bucket=GlobalAdmissionLimiter(clock=lambda:0.)
    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(lambda _:bucket.allow(),range(100)))==5


def test_explicit_budget_and_rollback_clock_semantics_are_preserved():
    clock=[1.];bucket=GlobalAdmissionLimiter(rate=2,burst=3,clock=lambda:clock[0])
    assert sum(bucket.allow() for _ in range(10))==3
    clock[0]=.5;assert not bucket.allow()
    clock[0]=1.5;assert bucket.allow() and not bucket.allow()

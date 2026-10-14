from wifi_helper import history
from wifi_helper.watchdog import WAKE_GRACE, SleepDetector


class Clocks:
    def __init__(self):
        self.wall, self.awake = 1_000_000.0, 50.0

    def run(self, seconds: float) -> None:
        self.wall += seconds
        self.awake += seconds

    def sleep(self, seconds: float) -> None:
        self.wall += seconds


def detector(c: Clocks) -> SleepDetector:
    return SleepDetector(wall=lambda: c.wall, awake=lambda: c.awake)


def test_normal_running_and_long_checks_are_not_sleep():
    c = Clocks()
    d = detector(c)
    for _ in range(10):
        c.run(15)
        assert not d.slept()
    c.run(400)
    assert not d.slept()
    assert not d.settling()


def test_dark_wake_is_detected_and_settles_after_grace():
    c = Clocks()
    d = detector(c)
    c.run(15)
    assert not d.slept()
    before = c.wall
    c.sleep(2000)
    c.run(5)
    assert d.slept()
    assert d.slept_at == before
    assert d.settling()
    assert not d.slept()
    c.run(WAKE_GRACE + 1)
    assert not d.settling()


def test_grace_counts_awake_time_across_repeated_dark_wakes():
    c = Clocks()
    d = detector(c)
    for _ in range(20):
        c.sleep(1000)
        c.run(3)
        assert d.slept()
        assert d.settling()


def test_now_iso_accepts_a_timestamp():
    assert history.now_iso(0).startswith("1970-01-01") or history.now_iso(0).startswith("1969-12-31")

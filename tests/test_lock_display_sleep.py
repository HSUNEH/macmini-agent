import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from lock_display_sleep import LockTimer


class LockTimerTests(unittest.TestCase):
    def test_waits_five_seconds(self):
        timer = LockTimer()
        self.assertFalse(timer.update(True, 100))
        self.assertFalse(timer.update(True, 104.99))
        self.assertTrue(timer.update(True, 105))

    def test_unlock_cancels_and_next_lock_restarts(self):
        timer = LockTimer()
        timer.update(True, 100)
        self.assertFalse(timer.update(False, 103))
        self.assertFalse(timer.update(True, 110))
        self.assertFalse(timer.update(True, 114))
        self.assertTrue(timer.update(True, 115))

    def test_once_per_lock_allows_waking_to_enter_password(self):
        timer = LockTimer()
        timer.update(True, 0)
        self.assertTrue(timer.update(True, 5))
        timer.fired = True
        self.assertFalse(timer.update(True, 500))
        timer.update(False, 501)
        timer.update(True, 502)
        self.assertTrue(timer.update(True, 507))

    def test_never_sleeps_unlocked(self):
        timer = LockTimer()
        for now in (0, 5, 100, 10000):
            self.assertFalse(timer.update(False, now))


if __name__ == '__main__':
    unittest.main()

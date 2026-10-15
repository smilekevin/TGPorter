"""Network retry delays retain recovery windows and have bounded jitter."""

import unittest
from unittest.mock import patch

from module.retry_policy import network_backoff


class RetryPolicyTests(unittest.TestCase):
    def test_recovery_windows_and_cap(self):
        for base, upper in ((10, 15), (15, 22), (25, 30), (30, 30), (60, 30),
                            (120, 30), (300, 30), (600, 30), (960, 30)):
            with self.subTest(base=base):
                with patch("module.retry_policy.random.randint", side_effect=lambda a, b: b) as draw:
                    self.assertEqual(network_backoff(base), upper)
                    draw.assert_called_once_with(min(base, 30), upper)

    def test_jitter_can_retain_minimum_delay(self):
        with patch("module.retry_policy.random.randint", side_effect=lambda a, b: a):
            self.assertEqual(network_backoff(10), 10)

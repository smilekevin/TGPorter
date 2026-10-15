"""Bounded jitter for application-level download retries."""

import random


def network_backoff(base_seconds, cap=30):
    """Spread retries with up to 50% jitter, bounded by the delay cap."""
    lower = min(base_seconds, cap)
    return random.randint(lower, min(cap, lower + lower // 2))

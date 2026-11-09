"""Small, dependency-free statistics for `stats` and `audit`."""
from __future__ import annotations

import math
from collections import Counter

Z95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float, float]:
    """(rate, low, high) with the Wilson score interval. n=0 gives (0, 0, 1)."""
    if n <= 0:
        return 0.0, 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def fmt_rate(successes: int, n: int) -> str:
    """'79% (95% CI 72–85%, n=139)'."""
    if n <= 0:
        return "n/a (n=0)"
    p, lo, hi = wilson(successes, n)
    return f"{p:.0%} (95% CI {lo:.0%}–{hi:.0%}, n={n})"


def detection_floor(n: int) -> float:
    """Width of the 95% Wilson interval at p=0.5: differences smaller than this cannot be told apart
    from noise with a sample of n. About 0.36 at n=25."""
    if n <= 0:
        return 1.0
    _, lo, hi = _mid(n)
    return hi - lo


def _mid(n: int) -> tuple[float, float, float]:
    z = Z95
    p = 0.5
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, centre - half, centre + half


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for two raters over the same items (None if undefined)."""
    pairs = [(x, y) for x, y in zip(a, b) if x and y]
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for x, y in pairs if x == y) / n
    ca, cb = Counter(x for x, _ in pairs), Counter(y for _, y in pairs)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if expected >= 1:
        return 1.0 if observed == 1 else None
    return (observed - expected) / (1 - expected)

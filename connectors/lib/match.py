"""
String similarity utilities for record matching.

Used by:
  - deathcare_merge.py — name similarity check during spatial deduplication
"""

from __future__ import annotations


def edit_distance(a: str, b: str) -> int:
    """
    Standard dynamic-programming Levenshtein edit distance.

    Complexity: O(len(a) * len(b)).  Strings are assumed short (names),
    so this is acceptable without further optimisation.
    """
    m, n = len(a), len(b)
    # dp[j] holds the edit distance between a[:i] and b[:j].
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def name_similarity(a: str | None, b: str | None) -> float:
    """
    Levenshtein-based similarity ratio in [0, 1].

    Returns 0.0 when either name is empty — two unnamed sites have
    no name evidence to confirm they are the same place, so we do not
    award them a 'high' confidence merge.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    max_len = max(len(a), len(b))
    return 1.0 - edit_distance(a, b) / max_len

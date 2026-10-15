"""Replay corpus 与 runner。

    from tests.replay import build_corpus, run_corpus
"""

from __future__ import annotations

from .corpus import ReplayCase, build_corpus, corpus_stats
from .runner import ReplayReport, ReplayResult, run_case, run_corpus

__all__ = [
    "ReplayCase",
    "build_corpus",
    "corpus_stats",
    "ReplayResult",
    "ReplayReport",
    "run_case",
    "run_corpus",
]

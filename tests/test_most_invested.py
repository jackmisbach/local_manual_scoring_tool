"""Pick the score the scorer invested the most time in.

:func:`scoring.store.most_invested_entry_for_scorer` walks the local
score history and drives the per-sample pageload pre-fill plus
the "Load most-invested into form" banner button. It picks by
``minutes_spent_scoring`` (descending) so a quick revision doesn't
displace the time-rich prior submission, and falls back to 0 for
missing/non-numeric minutes so a single zero-minute entry still wins
when nothing else is around.
"""

from __future__ import annotations

from typing import Any

from scoring.store import (
    SavedScore,
    ScoreFile,
    most_invested_entry_for_scorer,
)
from tests.conftest import SCORER_EMAIL

OTHER_EMAIL = "bob@metr.org"


def _entry(
    *,
    minutes: float | None,
    saved_at: str,
    author: str = SCORER_EMAIL,
    value: float = 0.5,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"author": author}
    if minutes is not None:
        metadata["minutes_spent_scoring"] = minutes
    return {
        "scorer": "manual_score",
        "saved_at": saved_at,
        "score": {
            "value": value,
            "answer": None,
            "explanation": "",
            "history": [],
            "metadata": metadata,
        },
    }


def _file(entries: list[dict[str, Any]]) -> ScoreFile:
    return {"history": entries}  # type: ignore[typeddict-item]


def _meta(entry: SavedScore | None) -> dict[str, Any]:
    """Reach into ``entry.score.metadata`` for asserts, tolerating None layers."""
    if entry is None:
        return {}
    return (entry.get("score") or {}).get("metadata") or {}


def test_most_invested_entry_picks_highest_minutes_not_latest() -> None:
    """A 30-min first pass beats a 2-min revision even if the revision is newer."""
    score_file = _file(
        [
            _entry(minutes=30, saved_at="2026-05-27T10:00:00Z"),
            _entry(minutes=2, saved_at="2026-05-27T11:00:00Z"),
        ]
    )
    picked = most_invested_entry_for_scorer(score_file, SCORER_EMAIL)
    assert _meta(picked).get("minutes_spent_scoring") == 30


def test_most_invested_entry_breaks_ties_by_saved_at() -> None:
    """When two entries tie on minutes, prefer the later ``saved_at``."""
    score_file = _file(
        [
            _entry(minutes=10, saved_at="2026-05-27T10:00:00Z"),
            _entry(minutes=10, saved_at="2026-05-27T11:00:00Z"),
        ]
    )
    picked = most_invested_entry_for_scorer(score_file, SCORER_EMAIL)
    assert picked is not None
    assert picked.get("saved_at") == "2026-05-27T11:00:00Z"


def test_most_invested_entry_filters_by_author() -> None:
    """Other scorers' entries are invisible to a per-author selection."""
    score_file = _file(
        [
            _entry(minutes=60, saved_at="2026-05-27T09:00:00Z", author=OTHER_EMAIL),
            _entry(minutes=5, saved_at="2026-05-27T10:00:00Z", author=SCORER_EMAIL),
        ]
    )
    picked = most_invested_entry_for_scorer(score_file, SCORER_EMAIL)
    assert _meta(picked).get("author") == SCORER_EMAIL


def test_most_invested_entry_treats_missing_minutes_as_zero() -> None:
    """A single entry without ``minutes_spent_scoring`` is still selectable."""
    score_file = _file([_entry(minutes=None, saved_at="2026-05-27T10:00:00Z")])
    picked = most_invested_entry_for_scorer(score_file, SCORER_EMAIL)
    assert picked is not None


def test_most_invested_entry_returns_none_when_no_match() -> None:
    """Empty history (or no entries by this scorer) yields ``None``."""
    assert most_invested_entry_for_scorer(_file([]), SCORER_EMAIL) is None
    score_file = _file([_entry(minutes=10, saved_at="x", author=OTHER_EMAIL)])
    assert most_invested_entry_for_scorer(score_file, SCORER_EMAIL) is None

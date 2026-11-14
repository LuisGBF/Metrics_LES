from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.anomalies import detect_drops, describe_drops, shadowban_report
from src.analysis.baseline import expected_vs_actual, summary
from src.analysis.frame import add_derived
from src.analysis.timing import best_slots, slot_table
from src.db import add_metric_snapshot, connect, upsert_post


def make_posts(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["posted_at"] = pd.to_datetime(frame["posted_at"], utc=True)
    return add_derived(frame)


def test_add_derived_computes_engagement() -> None:
    posts = make_posts(
        [
            {
                "id": "x:1",
                "platform": "x",
                "format": "tweet",
                "posted_at": "2026-01-01T12:00:00Z",
                "views": 1000,
                "impressions": 1000,
                "likes": 50,
                "comments": 10,
                "shares": 20,
                "saves": 20,
                "followers": 5000,
            }
        ]
    )
    assert posts.loc[0, "engagement"] == 100
    assert posts.loc[0, "engagement_rate"] == pytest.approx(0.1)
    assert posts.loc[0, "reach_per_1k_followers"] == pytest.approx(200.0)


def test_slot_table_shrinks_small_samples() -> None:
    rows = []
    for i in range(20):
        rows.append(
            {
                "id": f"x:{i}",
                "platform": "x",
                "format": "tweet",
                "posted_at": f"2026-01-{i + 1:02d}T21:00:00Z",
                "views": 1000,
                "impressions": 1000,
                "likes": 100,
                "comments": 0,
                "shares": 0,
                "saves": 0,
                "followers": 5000,
            }
        )
    rows.append(
        {
            "id": "x:lucky",
            "platform": "x",
            "format": "tweet",
            "posted_at": "2026-01-21T03:00:00Z",
            "views": 1000,
            "impressions": 1000,
            "likes": 400,
            "comments": 0,
            "shares": 0,
            "saves": 0,
            "followers": 5000,
        }
    )
    posts = make_posts(rows)
    slots = slot_table(posts, min_posts=1, prior_weight=5.0)
    assert not slots.empty
    lucky = slots[(slots["hour"] == 0)]  # 03:00 UTC = 00:00 em Sao Paulo
    if not lucky.empty:
        assert lucky["shrunk"].iloc[0] < lucky["mean"].iloc[0]
    best = best_slots(slots, top=3)
    assert len(best) <= 3


def test_expected_vs_actual_flags_underperformer() -> None:
    rows = []
    for i in range(10):
        rows.append(
            {
                "id": f"x:{i}",
                "platform": "x",
                "format": "tweet",
                "posted_at": f"2026-01-{i + 1:02d}T12:00:00Z",
                "views": 1000,
                "impressions": 1000,
                "likes": 100,
                "comments": 0,
                "shares": 0,
                "saves": 0,
                "followers": 5000,
            }
        )
    rows.append(
        {
            "id": "x:bad",
            "platform": "x",
            "format": "tweet",
            "posted_at": "2026-01-11T12:00:00Z",
            "views": 1000,
            "impressions": 1000,
            "likes": 10,
            "comments": 0,
            "shares": 0,
            "saves": 0,
            "followers": 5000,
        }
    )
    result = expected_vs_actual(make_posts(rows), target="engagement_rate", min_history=3)
    bad = result[result["id"] == "x:bad"].iloc[0]
    assert bad["status"] == "abaixo do esperado"
    assert bad["ratio"] < -0.5
    info = summary(make_posts(rows))
    assert info["abaixo"] >= 1


def test_detect_drops_flags_sustained_fall() -> None:
    rows = []
    for i in range(20):
        views = 1000 if i < 14 else 200
        rows.append(
            {
                "id": f"youtube:{i}",
                "platform": "youtube",
                "format": "video",
                "posted_at": f"2026-01-{i + 1:02d}T12:00:00Z",
                "views": 1000,
                "impressions": views,
                "likes": views // 20,
                "comments": 0,
                "shares": 0,
                "saves": 0,
                "followers": 5000,
            }
        )
    posts = make_posts(rows)
    posts.loc[14:, "reach"] = 200
    result = detect_drops(posts, metric="reach", window=6, z_threshold=-2.0, consecutive=2)
    assert result["alert"].any()
    assert describe_drops(result)["status"] == "atencao"


def test_shadowban_report_without_data(tmp_path) -> None:
    conn = connect(tmp_path / "empty.db")
    report = shadowban_report(conn, "youtube")
    assert report["verdict"] == "sem dados"
    conn.close()

from __future__ import annotations

import pytest

from src.analysis.frame import load_posts
from src.importers.x_analytics import import_x_analytics, prepare_dataframe
import pandas as pd


def write_csv(path, text: str, encoding: str = "utf-8") -> None:
    path.write_text(text, encoding=encoding)


def test_import_english_export_uses_platform_engagements(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(
        csv_path,
        "Post id,Date,Post text,Post link,Impressions,Engagements,Engagement rate,"
        "Retweets,Replies,Likes,Bookmarks,URL clicks,User profile clicks\n"
        '111,2026-09-01 14:00:00,"Primeiro post",https://x.com/teste/status/111,'
        "1000,50,0.05,5,3,20,2,4,6\n"
        '112,2026-09-02 21:30:00,"Segundo post",,2000,120,0.06,10,8,30,4,10,12\n',
    )

    db_path = tmp_path / "analytics.db"
    summary = import_x_analytics(csv_path, "@teste", db_path=db_path, verbose=False)
    assert summary["posts"] == 2
    assert summary["impressions_total"] == 3000
    assert summary["engagements_total"] == 170

    conn = None
    from src.db import connect

    conn = connect(db_path)
    posts = load_posts(conn, "x", "x:teste")
    assert len(posts) == 2
    assert posts["reach"].tolist() == [1000.0, 2000.0]
    # Usa "Engagements" do export (50 e 120), nao a soma de likes/replies.
    assert posts["engagement"].tolist() == [50.0, 120.0]
    assert posts["engagement_rate"].iloc[0] == pytest.approx(0.05)
    assert posts["engagement_rate"].iloc[1] == pytest.approx(0.06)
    assert posts["url"].notna().all()
    conn.close()


def test_import_portuguese_export_with_semicolon_and_dayfirst(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(
        csv_path,
        "ID do post;Data;Texto do post;Impressões;Engajamentos;Curtidas;Respostas;"
        "Retweets;Salvos;Cliques no link\n"
        "222;05/11/2026 09:15;Olá mundo;500;25;10;2;1;1;3\n",
        encoding="utf-8-sig",
    )

    db_path = tmp_path / "analytics.db"
    import_x_analytics(csv_path, "teste", db_path=db_path, dayfirst=True, verbose=False)

    from src.db import connect

    conn = connect(db_path)
    row = conn.execute("SELECT posted_at, text FROM posts").fetchone()
    assert row["posted_at"].startswith("2026-11-05T09:15")
    assert row["text"] == "Olá mundo"
    conn.close()


def test_reimport_is_idempotent(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(
        csv_path,
        "Post id,Date,Impressions,Engagements,Likes\n"
        "333,2026-10-01 10:00:00,800,40,10\n",
    )
    db_path = tmp_path / "analytics.db"
    import_x_analytics(csv_path, "@teste", db_path=db_path, verbose=False)
    import_x_analytics(csv_path, "@teste", db_path=db_path, verbose=False)

    from src.db import connect

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    conn.close()


def test_rows_without_valid_date_are_skipped(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(
        csv_path,
        "Post id,Date,Impressions,Engagements\n"
        "444,2026-10-01 10:00:00,100,5\n"
        "445,data-invalida,200,10\n",
    )
    summary = import_x_analytics(csv_path, "@teste", dry_run=True, verbose=False)
    assert summary["rows_imported"] == 1
    assert any("sem data valida" in warning for warning in summary["warnings"])


def test_dry_run_does_not_touch_database(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(
        csv_path,
        "Post id,Date,Impressions,Engagements\n"
        "555,2026-10-01 10:00:00,100,5\n",
    )
    db_path = tmp_path / "analytics.db"
    import_x_analytics(csv_path, "@teste", db_path=db_path, dry_run=True, verbose=False)
    assert not db_path.exists()


def test_missing_required_columns_raises(tmp_path) -> None:
    csv_path = tmp_path / "posts.csv"
    write_csv(csv_path, "Foo,Bar\n1,2\n")
    with pytest.raises(ValueError, match="data"):
        import_x_analytics(csv_path, "@teste", dry_run=True, verbose=False)


def test_prepare_dataframe_builds_stable_id_without_link() -> None:
    frame = pd.DataFrame(
        {
            "Date": ["2026-09-01 14:00:00"],
            "Post text": ["sem link"],
            "Impressions": [10],
        }
    )
    prepared, _ = prepare_dataframe(frame, "@teste")
    assert prepared["post_id"].iloc[0].startswith("imp-")
    assert prepared["url"].iloc[0] is None

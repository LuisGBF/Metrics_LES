from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

from ..db import add_account_snapshot, add_metric_snapshot, connect, now_iso, upsert_post
from ..profiles import get_or_create_profile

_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")

# O export do analytics.x.com muda o nome das colunas conforme o idioma e a versao.
# Os aliases sao comparados depois de normalizar (minusculas, sem acento, sem pontuacao).
_ALIASES_RAW: dict[str, list[str]] = {
    "post_id": ["Post id", "Post ID", "Tweet id", "Tweet ID", "ID do post", "ID del post", "id"],
    "posted_at": [
        "Date",
        "Time",
        "Post date",
        "Date time",
        "Data",
        "Data e hora",
        "Fecha",
        "Created at",
    ],
    "text": ["Post text", "Tweet text", "Text", "Texto do post", "Texto", "Conteudo", "Post"],
    "url": ["Post link", "Tweet link", "Permalink", "Link do post", "Link", "URL"],
    "impressions": [
        "Impressions",
        "Impressoes",
        "Impresiones",
        "Views",
        "Visualizacoes",
        "Impressions organic",
    ],
    "engagements": ["Engagements", "Engajamentos", "Engajamento", "Interactions", "Interacoes", "Interacciones"],
    "engagement_rate": ["Engagement rate", "Taxa de engajamento", "Tasa de interaccion"],
    "likes": ["Likes", "Curtidas", "Me gusta"],
    "replies": ["Replies", "Respostas", "Respuestas"],
    "reposts": ["Retweets", "Reposts", "Retuitadas", "Repostagens"],
    "quotes": ["Quotes", "Quote tweets", "Citacoes", "Citas"],
    "bookmarks": ["Bookmarks", "Saves", "Salvos", "Marcadores"],
    "profile_clicks": ["User profile clicks", "Profile clicks", "Cliques no perfil", "Clics en el perfil"],
    "url_clicks": ["URL clicks", "Link clicks", "Cliques no link", "Clics en el enlace"],
    "hashtag_clicks": ["Hashtag clicks", "Cliques em hashtag", "Clics en hashtag"],
    "detail_expands": ["Detail expands", "Expansoes de detalhes", "Expansiones de detalles"],
    "follows": ["Follows", "New follows", "Novos seguidores", "Seguidores nuevos"],
}


def _normalize_header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())


_COLUMN_ALIASES = {
    target: [_normalize_header(alias) for alias in aliases]
    for target, aliases in _ALIASES_RAW.items()
}


def _read_csv(path: str | Path) -> pd.DataFrame:
    attempts = ("utf-8-sig", "utf-16", "latin-1")
    last_error: Exception | None = None
    for encoding in attempts:
        try:
            frame = pd.read_csv(path, sep=None, engine="python", encoding=encoding)
        except (UnicodeDecodeError, UnicodeError, pd.errors.ParserError) as error:
            last_error = error
            continue
        if frame.shape[1] > 1 or encoding == attempts[-1]:
            return frame
    raise ValueError(f"Nao foi possivel ler o CSV: {last_error}")


def _map_columns(frame: pd.DataFrame) -> dict[str, str]:
    normalized = {_normalize_header(column): column for column in frame.columns}
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for target, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            source = normalized.get(alias)
            if source is not None and source not in used:
                mapping[target] = source
                used.add(source)
                break
    return mapping


def _to_number(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(r"[^0-9.\-]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def _parse_dates(series: pd.Series, dayfirst: bool) -> pd.Series:
    values = series.astype(str).str.strip()
    parsed = pd.to_datetime(values, errors="coerce", utc=True, dayfirst=dayfirst)
    if parsed.isna().mean() > 0.5:
        parsed = pd.to_datetime(
            values, errors="coerce", utc=True, format="%a %b %d %H:%M:%S %z %Y"
        )
    return parsed


def _text_or_none(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _post_id(link: Any, raw_id: Any, posted_at: Any, text: str | None) -> str:
    if link is not None and not (isinstance(link, float) and pd.isna(link)):
        match = _STATUS_RE.search(str(link))
        if match:
            return match.group(1)

    if raw_id is not None and not (isinstance(raw_id, float) and pd.isna(raw_id)):
        candidate = re.sub(r"\.0$", "", str(raw_id).strip())
        if candidate.isdigit():
            return candidate

    digest = hashlib.sha1(f"{posted_at}|{text or ''}".encode("utf-8")).hexdigest()[:16]
    return f"imp-{digest}"


def _as_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(round(float(value)))


def _sum_optional(first: Any, second: Any) -> int | None:
    if pd.isna(first) and pd.isna(second):
        return None
    return int((0 if pd.isna(first) else first) + (0 if pd.isna(second) else second))


def prepare_dataframe(
    frame: pd.DataFrame, handle: str, dayfirst: bool = False
) -> tuple[pd.DataFrame, list[str]]:
    """Normaliza o export do X em um DataFrame com tipos e ids prontos para o banco."""
    mapping = _map_columns(frame)
    warnings: list[str] = []

    if "posted_at" not in mapping:
        raise ValueError(
            "Coluna de data nao encontrada no CSV. Exporte o 'Post activity' do analytics.x.com."
        )
    if "impressions" not in mapping and "engagements" not in mapping:
        raise ValueError("Nenhuma metrica reconhecida (impressions/engagements) no CSV.")

    unknown = [
        column for column in frame.columns if _normalize_header(column) not in {"", None} and column not in mapping.values()
    ]
    if unknown:
        warnings.append(
            "Colunas ignoradas: " + ", ".join(str(column) for column in unknown[:8])
        )

    posted_at = _parse_dates(frame[mapping["posted_at"]], dayfirst=dayfirst)
    if posted_at.isna().any():
        warnings.append(f"{int(posted_at.isna().sum())} linha(s) sem data valida serao ignoradas.")

    text = frame[mapping["text"]] if "text" in mapping else pd.Series([None] * len(frame))
    link = frame[mapping["url"]] if "url" in mapping else pd.Series([None] * len(frame))
    raw_id = frame[mapping["post_id"]] if "post_id" in mapping else pd.Series([None] * len(frame))

    numbers: dict[str, pd.Series] = {}
    for key in (
        "impressions",
        "engagements",
        "engagement_rate",
        "likes",
        "replies",
        "reposts",
        "quotes",
        "bookmarks",
        "profile_clicks",
        "url_clicks",
        "hashtag_clicks",
        "detail_expands",
        "follows",
    ):
        if key in mapping:
            numbers[key] = _to_number(frame[mapping[key]])
        else:
            numbers[key] = pd.Series([float("nan")] * len(frame))

    if "impressions" not in mapping:
        warnings.append("Sem coluna de impressoes: o alcance nao podera ser calculado.")
    if "engagements" not in mapping:
        warnings.append("Sem coluna de engagements: a taxa usara likes+respostas+reposts+salvos.")

    rows: list[dict[str, Any]] = []
    handles = str(handle).lstrip("@")
    for position in range(len(frame)):
        timestamp = posted_at.iloc[position]
        if pd.isna(timestamp):
            continue

        post_text = _text_or_none(text.iloc[position])
        post_id = _post_id(link.iloc[position], raw_id.iloc[position], timestamp, post_text)
        url = _text_or_none(link.iloc[position])
        if url is None and post_id.isdigit():
            url = f"https://x.com/{handles}/status/{post_id}"

        rows.append(
            {
                "post_id": post_id,
                "posted_at": timestamp.isoformat(),
                "text": post_text,
                "url": url,
                "views": _as_int(numbers["impressions"].iloc[position]),
                "impressions": _as_int(numbers["impressions"].iloc[position]),
                "engagements": _as_int(numbers["engagements"].iloc[position]),
                "likes": _as_int(numbers["likes"].iloc[position]),
                "comments": _as_int(numbers["replies"].iloc[position]),
                "shares": _sum_optional(
                    numbers["reposts"].iloc[position], numbers["quotes"].iloc[position]
                ),
                "saves": _as_int(numbers["bookmarks"].iloc[position]),
                "engagement_rate_exported": (
                    None
                    if pd.isna(numbers["engagement_rate"].iloc[position])
                    else float(numbers["engagement_rate"].iloc[position])
                ),
                "profile_clicks": _as_int(numbers["profile_clicks"].iloc[position]),
                "url_clicks": _as_int(numbers["url_clicks"].iloc[position]),
                "hashtag_clicks": _as_int(numbers["hashtag_clicks"].iloc[position]),
                "detail_expands": _as_int(numbers["detail_expands"].iloc[position]),
                "follows": _as_int(numbers["follows"].iloc[position]),
            }
        )

    prepared = pd.DataFrame(rows)
    if prepared.empty:
        return prepared, warnings
    prepared = prepared.sort_values("posted_at").reset_index(drop=True)
    return prepared, warnings


def import_x_analytics(
    path: str | Path,
    handle: str,
    db_path: str | Path | None = None,
    followers: int | None = None,
    dayfirst: bool = False,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Importa o CSV do Post Activity Dashboard (analytics.x.com) sem tocar na API."""
    frame = _read_csv(path)
    prepared, warnings = prepare_dataframe(frame, handle, dayfirst=dayfirst)
    summary: dict[str, Any] = {
        "rows_read": len(frame),
        "rows_imported": len(prepared),
        "posts": len(prepared),
        "warnings": warnings,
    }
    if prepared.empty:
        raise SystemExit("Nenhuma linha valida no CSV. Confira se exportou o 'Post activity'.")

    impressions = prepared["impressions"].dropna()
    engagements = prepared["engagements"].dropna()
    summary["impressions_total"] = int(impressions.sum())
    summary["engagements_total"] = int(engagements.sum())
    summary["period"] = (prepared["posted_at"].min(), prepared["posted_at"].max())

    if dry_run:
        if verbose:
            print(
                f"[import x] {summary['posts']} posts | {summary['impressions_total']} impressoes | "
                f"{summary['engagements_total']} engajamentos | periodo {summary['period'][0]} a "
                f"{summary['period'][1]}"
            )
            for warning in warnings:
                print(f"[aviso] {warning}")
        return summary

    conn = connect(db_path)
    profile = get_or_create_profile(conn, "x", handle, mode="owned")
    if followers is not None:
        add_account_snapshot(
            conn,
            "x",
            profile_id=profile["id"],
            followers=int(followers),
            extra={"source": "analytics.x.com"},
        )

    stamp = now_iso()
    for row in prepared.itertuples(index=False):
        upsert_post(
            conn,
            {
                "id": f"x:{row.post_id}",
                "profile_id": profile["id"],
                "platform": "x",
                "platform_id": row.post_id,
                "posted_at": row.posted_at,
                "format": "tweet",
                "title": None,
                "text": row.text,
                "url": row.url,
                "duration_s": None,
                "extra": {"source": "analytics.x.com"},
            },
        )
        add_metric_snapshot(
            conn,
            f"x:{row.post_id}",
            collected_at=stamp,
            views=row.views,
            impressions=row.impressions,
            engagements=row.engagements,
            likes=row.likes,
            comments=row.comments,
            shares=row.shares,
            saves=row.saves,
            followers=followers,
            raw={
                "source": "analytics.x.com",
                "engagement_rate": row.engagement_rate_exported,
                "profile_clicks": row.profile_clicks,
                "url_clicks": row.url_clicks,
                "hashtag_clicks": row.hashtag_clicks,
                "detail_expands": row.detail_expands,
                "follows": row.follows,
            },
        )

    conn.commit()
    conn.close()
    summary["profile"] = profile["id"]

    if verbose:
        print(
            f"[import x] {summary['posts']} posts importados para {profile['id']} "
            f"({summary['impressions_total']} impressoes, {summary['engagements_total']} engajamentos)"
        )
        for warning in warnings:
            print(f"[aviso] {warning}")
    return summary

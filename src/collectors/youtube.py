from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import SECRETS_DIR, env
from ..db import (
    add_account_snapshot,
    add_audience,
    add_breakdown,
    add_metric_snapshot,
    connect,
    upsert_post,
)
from ..profiles import YOUTUBE_CHANNEL_ID, get_or_create_profile, normalize_handle

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

TOKEN_PATH = SECRETS_DIR / "youtube_token.json"
CLIENT_SECRET_PATH = SECRETS_DIR / "client_secret.json"
DEFAULT_HANDLE = env("YT_CHANNEL_HANDLE")

CORE_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)
THUMBNAIL_METRICS = "videoThumbnailImpressions,videoThumbnailImpressionsClickRate"

_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
)


def parse_duration(value: str | None) -> int | None:
    """Converte a duracao ISO-8601 do YouTube (PT1H2M3S) em segundos."""
    if not value:
        return None
    match = _DURATION_RE.fullmatch(value)
    if not match:
        return None
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return int(round(total))


def _client_config() -> dict[str, Any]:
    if CLIENT_SECRET_PATH.exists():
        return json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
    client_id = env("GOOGLE_CLIENT_ID")
    client_secret = env("GOOGLE_CLIENT_SECRET")
    if client_id and client_secret:
        return {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    raise SystemExit(
        "Credenciais do Google ausentes. Coloque .secrets/client_secret.json "
        "ou defina GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET no .env"
    )


def get_credentials() -> Credentials:
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except ValueError:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _public_client() -> Any:
    api_key = env("YT_API_KEY")
    if not api_key:
        raise SystemExit(
            "Para analisar outro canal defina YT_API_KEY no .env "
            "(console.cloud.google.com -> Credenciais -> Chave de API)."
        )
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _query_all(
    analytics: Any,
    metrics: str,
    dimensions: str,
    start: str,
    end: str,
    max_rows: int = 200,
) -> tuple[list[dict[str, Any]], list[list[Any]]]:
    """Executa reports.query paginando por startIndex quando necessario."""
    rows: list[list[Any]] = []
    headers: list[dict[str, Any]] = []
    start_index = 1
    supports_paging = True
    while True:
        params = dict(
            ids="channel==MINE",
            startDate=start,
            endDate=end,
            metrics=metrics,
            dimensions=dimensions,
            maxResults=min(max_rows, 200),
        )
        if supports_paging:
            params["startIndex"] = start_index
        try:
            response = analytics.reports().query(**params).execute()
        except TypeError:
            # Versao da lib/API sem startIndex: desliga a paginacao em vez de
            # repetir a mesma pagina ate o limite.
            supports_paging = False
            params.pop("startIndex", None)
            response = analytics.reports().query(**params).execute()
        except HttpError as error:
            if start_index > 1:
                break
            raise error

        headers = response.get("columnHeaders", []) or headers
        batch = response.get("rows", []) or []
        rows.extend(batch)
        if not supports_paging or len(batch) < params["maxResults"]:
            break
        start_index += params["maxResults"]
        if start_index > 5000:
            break
    return headers, rows


def _date_range() -> tuple[str, str]:
    start = env("YT_START_DATE", "2008-01-01") or "2008-01-01"
    return start, date.today().isoformat()


def _fetch_video_ids(youtube: Any, uploads: str, limit: int | None) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while True:
        page = (
            youtube.playlistItems()
            .list(part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token)
            .execute()
        )
        for item in page.get("items", []):
            ids.append(item["contentDetails"]["videoId"])
        token = page.get("nextPageToken")
        if not token or (limit and len(ids) >= limit):
            break
    return ids[:limit] if limit else ids


def _resolve_channel(youtube: Any, handle: str) -> dict[str, Any]:
    """Descobre o canal publico a partir de @handle, URL, username legado ou ID."""
    normalized = normalize_handle("youtube", handle)
    if YOUTUBE_CHANNEL_ID.fullmatch(normalized):
        response = youtube.channels().list(
            part="snippet,contentDetails,statistics", id=normalized
        ).execute()
    else:
        response = youtube.channels().list(
            part="snippet,contentDetails,statistics", forHandle=normalized
        ).execute()
        if not response.get("items"):
            response = youtube.channels().list(
                part="snippet,contentDetails,statistics", forUsername=normalized
            ).execute()

    items = response.get("items") or []
    if not items:
        raise SystemExit(
            f"Canal '{handle}' nao encontrado. Confira o @handle, a URL ou o ID (UC...)."
        )
    return items[0]


def collect(
    handle: str | None = None,
    mode: str = "auto",
    limit: int | None = None,
    db_path: str | Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Coleta o canal.

    - `owned`/auto sem handle: OAuth, com metricas privadas (impressoes, CTR, audiencia).
    - `public`/auto com handle: API key, somente dados publicos do canal informado.
    """
    if mode not in ("auto", "owned", "public"):
        raise ValueError(f"Modo invalido: {mode!r}. Use 'auto', 'owned' ou 'public'.")

    use_public = mode == "public" or (
        mode == "auto" and handle not in (None, "", "me", "own")
    )
    if use_public:
        return _collect_public(handle or DEFAULT_HANDLE, limit=limit, db_path=db_path, verbose=verbose)
    if handle not in (None, "", "me", "own") and mode == "owned" and verbose:
        print(
            "[youtube] modo owned ignora --profile: o OAuth so le o canal da conta autorizada."
        )
    return _collect_owned(limit=limit, db_path=db_path, verbose=verbose)


def _collect_public(
    handle: str | None,
    limit: int | None = None,
    db_path: str | Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    if not handle:
        raise SystemExit(
            "Informe --profile @canal (ou defina YT_CHANNEL_HANDLE no .env) "
            "para coletar um canal publico."
        )

    youtube = _public_client()
    channel = _resolve_channel(youtube, handle)
    channel_id = channel["id"]
    snippet = channel.get("snippet", {})
    stats = channel.get("statistics", {})
    subscribers = _int(stats.get("subscriberCount"))
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]

    conn = connect(db_path)
    profile = get_or_create_profile(
        conn,
        "youtube",
        handle,
        mode="public",
        external_id=channel_id,
        label=snippet.get("title"),
    )
    add_account_snapshot(
        conn,
        "youtube",
        profile_id=profile["id"],
        followers=subscribers,
        posts_count=_int(stats.get("videoCount")),
        extra={"channel_id": channel_id, "title": snippet.get("title"), "mode": "public"},
    )

    video_ids = _fetch_video_ids(youtube, uploads, limit)
    if verbose:
        print(f"[youtube] canal publico {snippet.get('title') or handle}: {len(video_ids)} videos")

    written = 0
    for offset in range(0, len(video_ids), 50):
        chunk = video_ids[offset : offset + 50]
        response = (
            youtube.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(chunk), maxResults=50)
            .execute()
        )
        for video in response.get("items", []):
            vid = video["id"]
            video_snippet = video.get("snippet", {})
            vstats = video.get("statistics", {})
            duration = parse_duration(video.get("contentDetails", {}).get("duration"))
            fmt = "short" if duration and duration <= 60 else "video"

            upsert_post(
                conn,
                {
                    "id": f"youtube:{vid}",
                    "profile_id": profile["id"],
                    "platform": "youtube",
                    "platform_id": vid,
                    "posted_at": video_snippet.get("publishedAt"),
                    "format": fmt,
                    "title": video_snippet.get("title"),
                    "text": (video_snippet.get("description") or "")[:4000],
                    "url": f"https://youtu.be/{vid}",
                    "duration_s": duration,
                    "extra": {
                        "tags": (video_snippet.get("tags") or [])[:25],
                        "mode": "public",
                    },
                },
            )
            # Sem Analytics API nao existem impressoes, CTR, retencao ou audiencia.
            add_metric_snapshot(
                conn,
                f"youtube:{vid}",
                views=_int(vstats.get("viewCount")),
                likes=_int(vstats.get("likeCount")),
                comments=_int(vstats.get("commentCount")),
                followers=subscribers,
            )
            written += 1

    conn.commit()
    conn.close()
    if verbose:
        print(f"[youtube] {written} videos publicos gravados (perfil {profile['id']})")
    return {
        "posts": written,
        "videos_seen": len(video_ids),
        "profile": profile["id"],
        "mode": "public",
    }


def _collect_owned(
    limit: int | None = None,
    db_path: str | Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)

    channel_resp = youtube.channels().list(
        part="snippet,contentDetails,statistics", mine=True
    ).execute()
    items = channel_resp.get("items") or []
    if not items:
        raise SystemExit("Nenhum canal do YouTube encontrado para esta conta Google.")
    channel = items[0]
    channel_id = channel["id"]
    snippet = channel.get("snippet", {})
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    stats = channel.get("statistics", {})
    subscribers = _int(stats.get("subscriberCount"))

    conn = connect(db_path)
    profile = get_or_create_profile(
        conn,
        "youtube",
        snippet.get("customUrl") or channel_id,
        mode="owned",
        external_id=channel_id,
        label=snippet.get("title"),
    )

    add_account_snapshot(
        conn,
        "youtube",
        profile_id=profile["id"],
        followers=subscribers,
        posts_count=_int(stats.get("videoCount")),
        extra={"channel_id": channel_id, "title": snippet.get("title"), "mode": "owned"},
    )

    video_ids = _fetch_video_ids(youtube, uploads, limit)
    if verbose:
        print(f"[youtube] {len(video_ids)} videos encontrados (perfil {profile['id']})")

    start, end = _date_range()

    # Metricas por video.
    core: dict[str, dict[str, Any]] = {}
    try:
        headers, rows = _query_all(analytics, CORE_METRICS, "video", start, end)
    except HttpError:
        start = (date.today() - timedelta(days=365)).isoformat()
        headers, rows = _query_all(analytics, CORE_METRICS, "video", start, end)
    index = {header["name"]: i for i, header in enumerate(headers)}
    for row in rows:
        vid = row[index["video"]]
        core[vid] = {
            "views": _int(row[index["views"]]),
            "watch_time_s": (row[index["estimatedMinutesWatched"]] or 0) * 60,
            "avg_view_duration_s": row[index["averageViewDuration"]],
            "avg_view_percentage": row[index["averageViewPercentage"]],
            "likes": _int(row[index["likes"]]),
            "comments": _int(row[index["comments"]]),
            "shares": _int(row[index["shares"]]),
            "subscribers_gained": _int(row[index["subscribersGained"]]),
            "subscribers_lost": _int(row[index["subscribersLost"]]),
        }

    # Impressoes / CTR do thumbnail (nem toda conta tem esses dados).
    try:
        headers, rows = _query_all(analytics, THUMBNAIL_METRICS, "video", start, end)
        index = {header["name"]: i for i, header in enumerate(headers)}
        for row in rows:
            vid = row[index["video"]]
            core.setdefault(vid, {})
            core[vid]["impressions"] = _int(row[index["videoThumbnailImpressions"]])
            core[vid]["ctr"] = row[index["videoThumbnailImpressionsClickRate"]]
    except HttpError:
        if verbose:
            print("[youtube] impressoes/CTR indisponiveis para este canal")

    # Breakdowns: origem de trafego e inscritos vs nao inscritos.
    breakdowns: dict[str, dict[str, dict[str, float]]] = {}
    for dimension in ("insightTrafficSourceType", "subscribedStatus"):
        try:
            headers, rows = _query_all(analytics, "views", f"video,{dimension}", start, end)
        except HttpError:
            continue
        index = {header["name"]: i for i, header in enumerate(headers)}
        for row in rows:
            vid = row[index["video"]]
            bucket = row[index[dimension]]
            breakdowns.setdefault(vid, {}).setdefault(dimension, {})
            breakdowns[vid][dimension][bucket] = breakdowns[vid][dimension].get(bucket, 0) + (
                row[index["views"]] or 0
            )

    # Detalhes dos videos (Data API) e gravacao.
    written = 0
    for offset in range(0, len(video_ids), 50):
        chunk = video_ids[offset : offset + 50]
        response = (
            youtube.videos()
            .list(part="snippet,contentDetails,statistics", id=",".join(chunk), maxResults=50)
            .execute()
        )
        for video in response.get("items", []):
            vid = video["id"]
            video_snippet = video.get("snippet", {})
            vstats = video.get("statistics", {})
            duration = parse_duration(video.get("contentDetails", {}).get("duration"))
            fmt = "short" if duration and duration <= 60 else "video"

            upsert_post(
                conn,
                {
                    "id": f"youtube:{vid}",
                    "profile_id": profile["id"],
                    "platform": "youtube",
                    "platform_id": vid,
                    "posted_at": video_snippet.get("publishedAt"),
                    "format": fmt,
                    "title": video_snippet.get("title"),
                    "text": (video_snippet.get("description") or "")[:4000],
                    "url": f"https://youtu.be/{vid}",
                    "duration_s": duration,
                    "extra": {"tags": (video_snippet.get("tags") or [])[:25], "mode": "owned"},
                },
            )

            metrics = dict(core.get(vid, {}))
            if metrics.get("views") is None:
                metrics["views"] = _int(vstats.get("viewCount"))
            if metrics.get("likes") is None:
                metrics["likes"] = _int(vstats.get("likeCount"))
            if metrics.get("comments") is None:
                metrics["comments"] = _int(vstats.get("commentCount"))
            metrics["followers"] = subscribers

            add_metric_snapshot(conn, f"youtube:{vid}", **metrics)

            for dimension, buckets in breakdowns.get(vid, {}).items():
                add_breakdown(conn, f"youtube:{vid}", dimension, buckets)
            written += 1

    conn.commit()

    # Perfil de audiencia (quando o publico esta assistindo).
    window_start = (date.today() - timedelta(days=364)).isoformat()
    try:
        headers, rows = _query_all(analytics, "views", "hour", window_start, end)
        index = {header["name"]: i for i, header in enumerate(headers)}
        add_audience(
            conn,
            "youtube",
            "hour",
            {str(int(row[index["hour"]])): row[index["views"]] for row in rows},
            profile_id=profile["id"],
        )
    except HttpError:
        if verbose:
            print("[youtube] perfil de horarios indisponivel")

    try:
        headers, rows = _query_all(analytics, "views", "day", window_start, end)
        index = {header["name"]: i for i, header in enumerate(headers)}
        per_weekday: dict[int, float] = {}
        for row in rows:
            day = datetime.fromisoformat(str(row[index["day"]])).date()
            per_weekday[day.weekday()] = per_weekday.get(day.weekday(), 0) + (
                row[index["views"]] or 0
            )
        add_audience(
            conn,
            "youtube",
            "weekday",
            {str(k): v for k, v in per_weekday.items()},
            profile_id=profile["id"],
        )
    except HttpError:
        if verbose:
            print("[youtube] perfil por dia da semana indisponivel")

    conn.commit()
    conn.close()
    if verbose:
        print(f"[youtube] {written} videos gravados no banco (perfil {profile['id']})")
    return {
        "posts": written,
        "videos_seen": len(video_ids),
        "profile": profile["id"],
        "mode": "owned",
    }

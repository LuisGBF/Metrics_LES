from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tweepy

from ..config import SECRETS_DIR, env, env_float
from ..db import add_account_snapshot, add_metric_snapshot, connect, upsert_post
from ..profiles import get_or_create_profile, normalize_handle

SCOPES = ["tweet.read", "users.read", "offline.access"]
TOKEN_PATH = SECRETS_DIR / "x_token.json"
REDIRECT_URI = env("X_REDIRECT_URI", "http://localhost:8080/callback") or "http://localhost:8080/callback"

TWEET_FIELDS = [
    "created_at",
    "public_metrics",
    "non_public_metrics",
    "organic_metrics",
    "referenced_tweets",
    "conversation_id",
    "lang",
]
BASIC_FIELDS = ["created_at", "public_metrics", "referenced_tweets", "conversation_id", "lang"]


def _handler() -> tweepy.OAuth2UserHandler:
    client_id = env("X_CLIENT_ID")
    if not client_id:
        raise SystemExit("Defina X_CLIENT_ID no .env (console.x.com -> App -> OAuth 2.0).")
    return tweepy.OAuth2UserHandler(
        client_id=client_id,
        client_secret=env("X_CLIENT_SECRET"),
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
    )


def _save_token(token: dict[str, Any]) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(token)
    if "expires_at" not in payload and payload.get("expires_in"):
        payload["expires_at"] = time.time() + int(payload["expires_in"])
    TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_token() -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        raise SystemExit("Token do X ausente. Rode sem --dry-run para autorizar no navegador.")
    return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))


def authorize(force: bool = False) -> dict[str, Any]:
    """Fluxo OAuth 2.0 (PKCE) do X, salvando o token em .secrets/x_token.json."""
    if not force and TOKEN_PATH.exists():
        token = _load_token()
        expires_at = float(token.get("expires_at") or 0)
        if expires_at > time.time() + 60:
            return token
        if token.get("refresh_token"):
            try:
                refreshed = _handler().refresh_token(token["refresh_token"])
                _save_token(refreshed)
                return refreshed
            except Exception:  # token expirado/revogado: cai para o fluxo interativo
                pass

    handler = _handler()
    print("\n1) Abra a URL abaixo no navegador e autorize o app:\n")
    print(handler.get_authorization_url())
    print(
        "\n2) Depois de autorizar, o navegador vai tentar abrir "
        f"{REDIRECT_URI} e provavelmente mostrar erro de conexao."
    )
    print("3) Copie a URL completa da barra de enderecos e cole aqui.\n")
    redirect_response = input("URL de redirecionamento: ").strip()
    token = handler.fetch_token(redirect_response)
    _save_token(token)
    return token


def _client(token: dict[str, Any]) -> tweepy.Client:
    return tweepy.Client(token["access_token"], wait_on_rate_limit=False)


def _public_client() -> tweepy.Client:
    """Cliente para ler perfis de terceiros: bearer token (app-only) ou OAuth do usuario."""
    bearer = env("X_BEARER_TOKEN")
    if bearer:
        return tweepy.Client(bearer_token=bearer, wait_on_rate_limit=False)
    return _client(authorize())


def _ref_types(tweet: Any) -> list[str]:
    refs = getattr(tweet, "referenced_tweets", None) or []
    types: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            types.append(ref.get("type"))
        else:
            types.append(getattr(ref, "type", None))
    return [t for t in types if t]


def _as_iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)


def _read_cost(owned: bool) -> float:
    if owned:
        return env_float("X_OWNED_READ_USD", 0.001)
    return env_float("X_THIRD_PARTY_READ_USD", 0.005)


def estimate_cost(limit: int, budget: float | None = None, owned: bool = True) -> dict[str, Any]:
    read_cost = _read_cost(owned)
    cap = env_float("X_BUDGET_USD", 2.0) if budget is None else budget
    return {
        "reads": float(limit),
        "read_cost_usd": read_cost,
        "estimated_usd": round(limit * read_cost, 4),
        "budget_usd": cap,
        "max_reads": int(cap / read_cost) if read_cost else 0,
        "mode": "owned" if owned else "public",
    }


def collect(
    handle: str | None = None,
    mode: str = "auto",
    limit: int = 200,
    db_path: str | Path | None = None,
    dry_run: bool = False,
    budget: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Coleta um perfil do X.

    - `owned`/auto sem handle: OAuth do dono, inclui metricas privadas (impressoes).
    - `public`/auto com handle: somente metricas publicas do perfil informado.
    """
    if mode not in ("auto", "owned", "public"):
        raise ValueError(f"Modo invalido: {mode!r}. Use 'auto', 'owned' ou 'public'.")

    use_public = mode == "public" or (
        mode == "auto" and handle not in (None, "", "me", "own")
    )
    if use_public:
        if not handle:
            raise SystemExit("Informe --profile @usuario para analisar outro perfil no X.")
        return _collect_public(
            handle, limit=limit, db_path=db_path, dry_run=dry_run, budget=budget, verbose=verbose
        )
    if handle not in (None, "", "me", "own") and mode == "owned" and verbose:
        print("[x] modo owned ignora --profile: o OAuth so le a conta autorizada.")
    return _collect_owned(
        limit=limit, db_path=db_path, dry_run=dry_run, budget=budget, verbose=verbose
    )


def _paginate(client: tweepy.Client, user_id: str, limit: int, fields: list[str]) -> list[Any]:
    tweets: list[Any] = []
    pagination_token: str | None = None
    while len(tweets) < limit:
        remaining = min(100, limit - len(tweets))
        if remaining < 5:
            break
        try:
            response = client.get_users_tweets(
                user_id,
                max_results=remaining,
                tweet_fields=fields,
                exclude=["retweets"],
                pagination_token=pagination_token,
            )
        except tweepy.Forbidden:
            # Conta/plano sem acesso a metricas privadas: tenta apenas as publicas.
            fields = BASIC_FIELDS
            response = client.get_users_tweets(
                user_id,
                max_results=remaining,
                tweet_fields=fields,
                exclude=["retweets"],
                pagination_token=pagination_token,
            )
        batch = list(response.data or [])
        tweets.extend(batch)
        pagination_token = response.meta.get("next_token") if response.meta else None
        if not pagination_token or not batch:
            break
    return tweets


def _store_tweets(
    conn: Any,
    profile: dict[str, Any],
    username: str,
    tweets: list[Any],
    followers: int | None,
    collect_private: bool,
) -> int:
    written = 0
    for tweet in tweets:
        types = _ref_types(tweet)
        fmt = "reply" if "replied_to" in types else "quote" if "quoted" in types else "tweet"
        pm = tweet.public_metrics or {}
        npm = (getattr(tweet, "non_public_metrics", None) or {}) if collect_private else {}
        om = (getattr(tweet, "organic_metrics", None) or {}) if collect_private else {}

        impressions = (
            npm.get("impression_count")
            or om.get("impression_count")
            or pm.get("impression_count")
        )

        upsert_post(
            conn,
            {
                "id": f"x:{tweet.id}",
                "profile_id": profile["id"],
                "platform": "x",
                "platform_id": str(tweet.id),
                "posted_at": _as_iso(tweet.created_at),
                "format": fmt,
                "title": None,
                "text": tweet.text,
                "url": f"https://x.com/{username}/status/{tweet.id}",
                "duration_s": None,
                "extra": {
                    "lang": getattr(tweet, "lang", None),
                    "conversation_id": getattr(tweet, "conversation_id", None),
                    "mode": "owned" if collect_private else "public",
                },
            },
        )
        add_metric_snapshot(
            conn,
            f"x:{tweet.id}",
            views=impressions,
            impressions=impressions,
            likes=pm.get("like_count"),
            comments=pm.get("reply_count"),
            shares=(pm.get("retweet_count") or 0) + (pm.get("quote_count") or 0),
            saves=pm.get("bookmark_count"),
            followers=followers,
            raw={
                "public": dict(pm),
                "non_public": dict(npm),
                "organic": dict(om),
            },
        )
        written += 1
    return written


def _collect_owned(
    limit: int = 200,
    db_path: str | Path | None = None,
    dry_run: bool = False,
    budget: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    plan = estimate_cost(limit, budget, owned=True)
    if verbose:
        print(
            f"[x] plano (perfil proprio): ate {limit} posts | custo estimado US$ "
            f"{plan['estimated_usd']:.2f} | orcamento US$ {plan['budget_usd']:.2f} "
            f"(max {plan['max_reads']} leituras)"
        )
    if dry_run:
        return plan

    if limit > plan["max_reads"]:
        limit = plan["max_reads"]
        if verbose:
            print(f"[x] limitado a {limit} leituras para respeitar o orcamento")

    token = authorize()
    client = _client(token)
    conn = connect(db_path)

    me = client.get_me(user_fields=["public_metrics"])
    if not me.data:
        raise SystemExit("Nao foi possivel ler o usuario autenticado no X.")
    user = me.data
    public = user.public_metrics or {}
    followers = public.get("followers_count")
    profile = get_or_create_profile(
        conn,
        "x",
        user.username,
        mode="owned",
        external_id=str(user.id),
        label=getattr(user, "name", None),
    )
    add_account_snapshot(
        conn,
        "x",
        profile_id=profile["id"],
        followers=followers,
        following=public.get("following_count"),
        posts_count=public.get("tweet_count"),
        extra={"username": user.username, "id": user.id, "mode": "owned"},
    )

    tweets = _paginate(client, user.id, limit, TWEET_FIELDS)
    written = _store_tweets(conn, profile, user.username, tweets, followers, collect_private=True)

    conn.commit()
    conn.close()
    if verbose:
        print(
            f"[x] {written} posts gravados (perfil {profile['id']}, custo aproximado US$ "
            f"{written * plan['read_cost_usd']:.3f})"
        )
    return {"posts": written, "profile": profile["id"], "mode": "owned", **plan}


def _collect_public(
    handle: str,
    limit: int = 200,
    db_path: str | Path | None = None,
    dry_run: bool = False,
    budget: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    username = normalize_handle("x", handle)
    plan = estimate_cost(limit, budget, owned=False)
    if verbose:
        print(
            f"[x] plano (perfil publico @{username}): ate {limit} posts | custo estimado US$ "
            f"{plan['estimated_usd']:.2f} | orcamento US$ {plan['budget_usd']:.2f} "
            f"(max {plan['max_reads']} leituras)"
        )
    if dry_run:
        return plan

    if limit > plan["max_reads"]:
        limit = plan["max_reads"]
        if verbose:
            print(f"[x] limitado a {limit} leituras para respeitar o orcamento")

    client = _public_client()
    response = client.get_user(username=username, user_fields=["public_metrics"])
    if not response.data:
        raise SystemExit(f"Perfil @{username} nao encontrado no X.")
    user = response.data
    public = user.public_metrics or {}
    followers = public.get("followers_count")

    conn = connect(db_path)
    profile = get_or_create_profile(
        conn,
        "x",
        user.username,
        mode="public",
        external_id=str(user.id),
        label=getattr(user, "name", None),
    )
    add_account_snapshot(
        conn,
        "x",
        profile_id=profile["id"],
        followers=followers,
        following=public.get("following_count"),
        posts_count=public.get("tweet_count"),
        extra={"username": user.username, "id": user.id, "mode": "public"},
    )

    tweets = _paginate(client, user.id, limit, BASIC_FIELDS)
    written = _store_tweets(conn, profile, user.username, tweets, followers, collect_private=False)

    conn.commit()
    conn.close()
    if verbose:
        print(
            f"[x] {written} posts publicos gravados (perfil {profile['id']}, custo aproximado US$ "
            f"{written * plan['read_cost_usd']:.3f})"
        )
    return {"posts": written, "profile": profile["id"], "mode": "public", **plan}

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tweepy

from ..config import SECRETS_DIR, env, env_float
from ..db import add_account_snapshot, add_metric_snapshot, connect, upsert_post

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


def estimate_cost(limit: int, budget: float | None = None) -> dict[str, float]:
    read_cost = env_float("X_OWNED_READ_USD", 0.001)
    cap = env_float("X_BUDGET_USD", 2.0) if budget is None else budget
    return {
        "reads": float(limit),
        "read_cost_usd": read_cost,
        "estimated_usd": round(limit * read_cost, 4),
        "budget_usd": cap,
        "max_reads": int(cap / read_cost) if read_cost else 0,
    }


def collect(
    limit: int = 200,
    db_path: str | Path | None = None,
    dry_run: bool = False,
    budget: float | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    plan = estimate_cost(limit, budget)
    if verbose:
        print(
            f"[x] plano: ate {limit} posts lidos | custo estimado US$ {plan['estimated_usd']:.2f} "
            f"| orcamento US$ {plan['budget_usd']:.2f} (max {plan['max_reads']} leituras)"
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
    add_account_snapshot(
        conn,
        "x",
        followers=public.get("followers_count"),
        following=public.get("following_count"),
        posts_count=public.get("tweet_count"),
        extra={"username": user.username, "id": user.id},
    )
    followers = public.get("followers_count")

    tweets: list[Any] = []
    pagination_token: str | None = None
    fields = TWEET_FIELDS
    while len(tweets) < limit:
        remaining = min(100, limit - len(tweets))
        if remaining < 5:
            break
        try:
            response = client.get_users_tweets(
                user.id,
                max_results=remaining,
                tweet_fields=fields,
                exclude=["retweets"],
                pagination_token=pagination_token,
            )
        except tweepy.Forbidden:
            # Conta/plano sem acesso a metricas privadas: tenta apenas as publicas.
            fields = BASIC_FIELDS
            response = client.get_users_tweets(
                user.id,
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

    written = 0
    for tweet in tweets:
        types = _ref_types(tweet)
        fmt = "reply" if "replied_to" in types else "quote" if "quoted" in types else "tweet"
        pm = tweet.public_metrics or {}
        npm = getattr(tweet, "non_public_metrics", None) or {}
        om = getattr(tweet, "organic_metrics", None) or {}

        impressions = npm.get("impression_count") or pm.get("impression_count") or om.get("impression_count")

        upsert_post(
            conn,
            {
                "id": f"x:{tweet.id}",
                "platform": "x",
                "platform_id": str(tweet.id),
                "posted_at": _as_iso(tweet.created_at),
                "format": fmt,
                "title": None,
                "text": tweet.text,
                "url": f"https://x.com/{user.username}/status/{tweet.id}",
                "duration_s": None,
                "extra": {"lang": getattr(tweet, "lang", None), "conversation_id": getattr(tweet, "conversation_id", None)},
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
                "public": pm,
                "non_public": {k: v for k, v in npm.items()},
                "organic": {k: v for k, v in om.items()},
            },
        )
        written += 1

    conn.commit()
    if verbose:
        print(f"[x] {written} posts gravados (custo aproximado US$ {written * plan['read_cost_usd']:.3f})")
    return {"posts": written, **plan}

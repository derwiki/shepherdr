"""Slack Web API adapter for a personal Slack client session.

This accepts a token and cookie pair from a private credentials file. It posts
as the signed-in user, so it is an opt-in alternative to a bot app.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


class SlackClientError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class ClientWebClient:
    def __init__(self, credentials_path: Path):
        self.credentials_path = credentials_path

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        credentials = json.loads(self.credentials_path.read_text())
        token, cookie = credentials.get("token", ""), credentials.get("cookie", "")
        if not token.startswith("xoxc-") or not cookie.startswith("xoxd-"):
            raise SlackClientError(
                "The Slack client credentials are missing or invalid"
            )
        values = {"token": token}
        for key, value in params.items():
            if value is not None:
                values[key] = (
                    str(value).lower() if isinstance(value, bool) else str(value)
                )
        request = Request(
            f"https://slack.com/api/{method}",
            data=urlencode(values).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": f"d={cookie}",
            },
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=20) as response:
                    result = json.load(response)
            except HTTPError as error:
                if error.code == 429 and attempt < 2:
                    time.sleep(max(float(error.headers.get("Retry-After", "1")), 1))
                    continue
                raise SlackClientError(f"Slack HTTP {error.code}") from error
            except URLError as error:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise SlackClientError(
                    f"Slack connection failed: {error.reason}"
                ) from error
            if result.get("ok"):
                return result
            if result.get("error") == "ratelimited" and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise SlackClientError(
                f"Slack {method} failed: {result.get('error', 'unknown')}",
                code=result.get("error"),
            )
        raise SlackClientError(f"Slack {method} did not complete")

    def auth_test(self) -> dict[str, Any]:
        return self.call("auth.test")

    def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("conversations.info", **kwargs)

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("chat.postMessage", **kwargs)

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("chat.update", **kwargs)

    def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("reactions.add", **kwargs)

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("conversations.replies", **kwargs)

    def search_messages(self, **kwargs: Any) -> dict[str, Any]:
        return self.call("search.messages", **kwargs)


class ClientMessageSearch:
    def __init__(self, client: ClientWebClient, channel: str):
        self.client = client
        info = client.conversations_info(channel=channel)
        self.name = info["channel"]["name"]
        self.channel = channel

    def since(self, scanned_at: float) -> list[dict[str, Any]]:
        # Search indexing can lag. Repeat a ten-minute window and de-duplicate
        # by Slack message timestamp in the bridge's SQLite store.
        cutoff = Decimal(str(scanned_at)) - Decimal(600)
        # Slack's `after:DATE` excludes DATE itself. Search from the prior
        # calendar day, then apply the exact timestamp cutoff below.
        date = (
            datetime.fromtimestamp(max(float(cutoff), 0), timezone.utc)
            - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        results = []
        page = 1
        while True:
            response = self.client.search_messages(
                query=f"in:{self.name} after:{date}",
                sort="timestamp",
                sort_dir="desc",
                count=100,
                page=page,
            )
            data = response.get("messages") or {}
            matches = data.get("matches") or []
            for match in matches:
                ts = match.get("ts")
                if not ts or Decimal(ts) < cutoff:
                    continue
                permalink = match.get("permalink") or ""
                thread_ts = (
                    match.get("thread_ts")
                    or parse_qs(urlparse(permalink).query).get("thread_ts", [None])[0]
                )
                if not thread_ts:
                    continue
                results.append(
                    {
                        "type": "message",
                        "channel": self.channel,
                        "user": match.get("user"),
                        "ts": ts,
                        "thread_ts": thread_ts,
                        "text": match.get("text", ""),
                    }
                )
            if not matches or Decimal(matches[-1].get("ts", "0")) < cutoff:
                break
            paging = data.get("paging") or {}
            if page >= int(paging.get("pages", 1)):
                break
            page += 1
        return sorted(results, key=lambda row: Decimal(row["ts"]))

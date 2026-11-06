"""Клиент YouGile REST API v2 (https://ru.yougile.com/api-v2).

У компании общий лимит — 50 запросов в минуту на всех, поэтому бот шлёт не чаще YG_RPM (по умолчанию 30)
и кеширует списки задач и пользователей.
"""
import asyncio
import logging
import os
import time

import aiohttp

log = logging.getLogger("yougile_bot.api")
BASE = os.environ.get("YG_BASE", "https://ru.yougile.com/api-v2")
RPM = int(os.environ.get("YG_RPM", "30"))


class YouGileError(Exception):
    pass


class YouGile:
    def __init__(self, key: str):
        self.key = key
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, object]] = {}

    async def _sess(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()

    async def call(self, method: str, path: str, params=None, body=None):
        """Один запрос с соблюдением лимита. На 429 ждёт и повторяет до 3 раз."""
        for attempt in range(4):
            async with self._lock:
                wait = 60 / RPM - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            s = await self._sess()
            try:
                async with s.request(method, BASE + path, params=params, json=body) as r:
                    if r.status == 429 and attempt < 3:
                        log.warning("YouGile 429, жду %s c", 20 * (attempt + 1))
                        await asyncio.sleep(20 * (attempt + 1))
                        continue
                    text = await r.text()
                    if r.status >= 400:
                        raise YouGileError(f"{method} {path}: {r.status} {text[:300]}")
                    return await r.json(content_type=None) if text else None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise YouGileError(f"{method} {path}: нет связи ({type(e).__name__})") from e
        raise YouGileError(f"{method} {path}: лимит запросов")

    async def _cached(self, key, ttl, loader):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = await loader()
        self._cache[key] = (time.monotonic(), value)
        return value

    def drop_cache(self, key=None):
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()

    async def _all(self, path, params=None, limit=1000):
        out, offset = [], 0
        while True:
            page = await self.call("GET", path, params={**(params or {}), "limit": limit, "offset": offset})
            content = page.get("content", []) if isinstance(page, dict) else []
            out += content
            if not (page or {}).get("paging", {}).get("next") or not content:
                return out
            offset += len(content)

    # --- справочники ---

    async def me(self):
        return await self._cached("me", 86400, lambda: self.call("GET", "/users/me"))

    async def users(self):
        return await self._cached("users", 1800, lambda: self._all("/users"))

    async def boards(self):
        return await self._cached("boards", 1800, lambda: self._all("/boards"))

    async def columns(self):
        return await self._cached("columns", 1800, lambda: self._all("/columns"))

    async def tasks(self):
        """Все задачи компании (без удалённых). Кеш 3 минуты: один клик — 1–3 запроса."""
        return await self._cached("tasks", 180, lambda: self._all("/task-list"))

    async def group_chats(self):
        return await self._cached("gchats", 300, lambda: self._all("/group-chats"))

    # --- задачи ---

    async def task_log(self, task_id, limit=100):
        page = await self.call("GET", f"/tasks/{task_id}/log", params={"limit": limit})
        return page.get("content", []) if isinstance(page, dict) else []

    async def messages(self, chat_id, since_ms, from_user=None):
        params = {"since": since_ms, "limit": 100}
        if from_user:
            params["fromUserId"] = from_user
        page = await self.call("GET", f"/chats/{chat_id}/messages", params=params)
        return page.get("content", []) if isinstance(page, dict) else []

    async def create_task(self, title, description, column_id, assigned, deadline_ms, key):
        body = {"title": title, "assigned": assigned, "idempotencyKey": key}
        if column_id:
            body["columnId"] = column_id
        if description:
            body["description"] = description.replace("\n", "<br>")
        if deadline_ms:
            body["deadline"] = {"deadline": deadline_ms, "withTime": False}
        res = await self.call("POST", "/tasks", body=body)
        self.drop_cache("tasks")
        return res

    async def send_message(self, chat_id, text, mention_ids=()):
        """Сообщение в чат задачи или групповой чат от имени владельца ключа.
        Упоминание — [!mention:id!], как его хранит YouGile (сверить на первом сообщении)."""
        import html as _html
        tags = " ".join(f"[!mention:{u}!]" for u in mention_ids)
        plain = f"{tags} {text}".strip()
        return await self.call("POST", f"/chats/{chat_id}/messages",
                               body={"text": plain, "textHtml": f"<p>{_html.escape(plain)}</p>", "label": ""})

    async def create_dm_chat(self, me, other, title):
        """Чат на двоих вместо личных сообщений (их API не даёт)."""
        res = await self.call("POST", "/group-chats", body={
            "title": title,
            "users": {me: {"notified": True}, other: {"notified": True}},
            "userRoleMap": {me: "admin", other: "user"},
            "roleConfigMap": {
                "admin": {"editProperties": True, "editAdmins": True, "editUsers": True, "sendMessages": True,
                          "removeMessages": True},
                "user": {"editProperties": False, "editAdmins": False, "editUsers": False, "sendMessages": True,
                         "removeMessages": False}}})
        self.drop_cache("gchats")
        return (res or {}).get("id")

    # --- вебхуки ---

    async def ensure_webhooks(self, url):
        """Подписка на события задач и сообщений. Повторно не создаёт."""
        res = await self.call("GET", "/webhooks") or []
        items = res.get("content", []) if isinstance(res, dict) else res
        have = {(w.get("url"), w.get("event")) for w in items if not w.get("deleted") and not w.get("disabled")}
        for event in ("task-.*", "chat_message-created"):
            if (url, event) not in have:
                await self.call("POST", "/webhooks", body={"url": url, "event": event, "filters": []})
                log.info("подписка на %s создана", event)

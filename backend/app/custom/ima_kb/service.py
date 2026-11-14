"""腾讯 ima 知识库：用官方开放接口在选定的知识库里搜索。

接口说明来自 ima 官方技能包（ima-skills 1.1.2）里的 ``knowledge-base/references/api.md``：

- 地址 ``https://ima.qq.com/openapi/wiki/v1/<接口名>``，全部是 POST + JSON；
- 请求头 ``ima-openapi-clientid`` 和 ``ima-openapi-apikey``（在 https://ima.qq.com/agent-interface 申请）；
- 返回 ``{"retcode": 0, "errmsg": "...", "data": {...}}``，retcode 不是 0 时 errmsg 就是给人看的原因；
- 这里只用到三个只读接口：``search_knowledge_base``（找知识库）、``get_addable_knowledge_base_list``
  （列出自己能写的知识库）、``search_knowledge``（在一个知识库里搜索，返回标题和命中的片段）。

官方接口没有“读取全文”，所以搜到的只有标题和一小段命中的内容。这个扩展不往知识库里写任何东西。

凭证和选中的知识库存在本机 ``<数据目录>/user_data/extensions/ima_kb/config.json``（只有本机用户可读）。
凭证不会从接口原样返回，只返回打了码的 Client ID。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://ima.qq.com/openapi/wiki/v1"
TIMEOUT_SECONDS = 20.0
MIN_INTERVAL_SECONDS = 0.5  # 两次请求之间至少隔这么久；官方有频控（错误码 110021）
MAX_SELECTED = 10
MAX_RESULTS_PER_BASE = 20
SNIPPET_CHARS = 400
CONTEXT_ITEMS = 8  # 交给 AI 参考的最多几条
CONTEXT_CACHE_SECONDS = 600
_SYMBOL = re.compile(r"(?<!\d)(\d{6})\.(SH|SZ|BJ)(?![A-Za-z0-9])")
_TAG = re.compile(r"</?[A-Za-z][^>]{0,40}>")

Post = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, str]]


class ImaError(Exception):
    """ima 接口没有成功。message 是可以直接给人看的原因。"""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, str]:
    import requests

    response = requests.post(url, headers=headers, json=body, timeout=timeout)
    return response.status_code, response.text


_post: Post = _http_post
_gate = threading.Lock()
_next_allowed = 0.0
_sleep: Callable[[float], None] = time.sleep


def set_transport(post: Post | None, sleep: Callable[[float], None] | None = None) -> None:
    """换掉真正发请求的函数和等待函数（测试用）；传 None 恢复。"""
    global _post, _sleep, _next_allowed
    _post = post or _http_post
    _sleep = sleep or time.sleep
    _next_allowed = 0.0


def mask(value: str) -> str:
    """只露出头尾几位，中间打码。"""
    value = value or ""
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def clean_snippet(text: Any) -> str:
    """命中的片段：去掉高亮用的标签，压成一行，太长就截断。"""
    plain = _TAG.sub("", str(text or ""))
    plain = " ".join(plain.split())
    return plain if len(plain) <= SNIPPET_CHARS else plain[:SNIPPET_CHARS] + "…"


class ImaService:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "config.json"
        self._lock = threading.Lock()
        self._context_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

    # ---------------- 配置 ----------------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self._path)
        self._context_cache.clear()

    def configured(self) -> bool:
        data = self._load()
        return bool(data.get("client_id") and data.get("api_key"))

    def config(self) -> dict[str, Any]:
        """给页面看的配置：凭证只给打了码的 Client ID，API Key 不返回。"""
        data = self._load()
        return {
            "configured": bool(data.get("client_id") and data.get("api_key")),
            "client_id_masked": mask(str(data.get("client_id") or "")),
            "selected": [b for b in data.get("selected", []) if isinstance(b, dict)],
            "use_in_ai": bool(data.get("use_in_ai", True)),
            "max_selected": MAX_SELECTED,
        }

    def set_credentials(self, client_id: str, api_key: str) -> dict[str, Any]:
        """保存凭证。先用它调一次接口，确认能用才保存。"""
        client_id, api_key = client_id.strip(), api_key.strip()
        if not client_id or not api_key:
            raise ImaError("Client ID 和 API Key 都要填。", 400)
        self._call("get_addable_knowledge_base_list", {"cursor": "", "limit": 1}, client_id, api_key)
        with self._lock:
            data = self._load()
            data.update(client_id=client_id, api_key=api_key)
            self._save(data)
        return self.config()

    def clear_credentials(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            data.pop("client_id", None)
            data.pop("api_key", None)
            self._save(data)
        return self.config()

    def set_selected(self, bases: list[dict[str, Any]]) -> dict[str, Any]:
        seen: dict[str, str] = {}
        for b in bases:
            ident, name = str(b.get("id") or "").strip(), str(b.get("name") or "").strip()
            if ident and ident not in seen:
                seen[ident] = name or ident
        if len(seen) > MAX_SELECTED:
            raise ImaError(f"最多选 {MAX_SELECTED} 个知识库（每个知识库搜一次就是一次请求）。", 400)
        with self._lock:
            data = self._load()
            data["selected"] = [{"id": k, "name": v} for k, v in seen.items()]
            self._save(data)
        return self.config()

    def set_use_in_ai(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            data["use_in_ai"] = bool(enabled)
            self._save(data)
        return self.config()

    # ---------------- 调接口 ----------------
    def _call(self, name: str, body: dict[str, Any], client_id: str | None = None,
              api_key: str | None = None) -> dict[str, Any]:
        global _next_allowed
        if client_id is None or api_key is None:
            data = self._load()
            client_id, api_key = str(data.get("client_id") or ""), str(data.get("api_key") or "")
        if not client_id or not api_key:
            raise ImaError("还没有填 ima 的凭证。请先到“知识库”页的设置里填 Client ID 和 API Key。", 400)
        headers = {"ima-openapi-clientid": client_id, "ima-openapi-apikey": api_key,
                   "Content-Type": "application/json"}
        with _gate:  # 一次只发一个，两次之间留间隔
            wait = _next_allowed - time.monotonic()
            if wait > 0:
                _sleep(wait)
            try:
                status, text = _post(f"{BASE_URL}/{name}", headers, body, TIMEOUT_SECONDS)
            except Exception as e:  # 网络层的任何失败都转成一句话
                raise ImaError(f"连不上 ima：{type(e).__name__}") from e
            finally:
                _next_allowed = time.monotonic() + MIN_INTERVAL_SECONDS
        if status in (401, 403):
            raise ImaError("ima 拒绝了这组凭证，请检查 Client ID 和 API Key 是否正确、是否已被撤销。", 400)
        if status != 200:
            raise ImaError(f"ima 返回了 HTTP {status}。")
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise ImaError("ima 返回的不是预期的格式。") from e
        if not isinstance(payload, dict):
            raise ImaError("ima 返回的不是预期的格式。")
        code = payload.get("retcode")
        if code != 0:
            message = str(payload.get("errmsg") or f"错误码 {code}")
            if code == 110021:
                message = "ima 提示请求太频繁，请过一会儿再试。"
            raise ImaError(f"ima：{message}", 400 if code in (110001, 110030) else 502)
        result = payload.get("data")
        return result if isinstance(result, dict) else {}

    def knowledge_bases(self, query: str = "") -> list[dict[str, str]]:
        """可以选的知识库。不填名称时列出自己有写入权限的（也就是自己的）；填了名称时按名称搜索
        （能搜到自己的，也能搜到订阅的、共享给自己的）。各是一次请求。"""
        found: dict[str, str] = {}
        if query.strip():
            data = self._call("search_knowledge_base", {"query": query.strip(), "cursor": "", "limit": 50})
            rows = data.get("info_list") or []
        else:
            data = self._call("get_addable_knowledge_base_list", {"cursor": "", "limit": 50})
            rows = data.get("addable_knowledge_base_list") or []
        for item in rows:
            if isinstance(item, dict) and item.get("id"):
                found[str(item["id"])] = str(item.get("name") or item["id"])
        return [{"id": k, "name": v} for k, v in found.items()]

    def search(self, query: str, base_ids: list[str] | None = None) -> dict[str, Any]:
        """在选中的知识库里搜索。一个知识库出错不影响别的，错误单独列出来。"""
        query = query.strip()
        if not query:
            raise ImaError("请填要搜索的内容。", 400)
        selected = self.config()["selected"]
        if base_ids:
            selected = [b for b in selected if b["id"] in set(base_ids)]
        if not selected:
            raise ImaError("还没有选知识库。请先到“知识库”页的设置里勾选要用的知识库。", 400)
        items: list[dict[str, str]] = []
        errors: list[str] = []
        for base in selected:
            try:
                data = self._call("search_knowledge", {"query": query, "cursor": "",
                                                       "knowledge_base_id": base["id"]})
            except ImaError as e:
                errors.append(f"{base['name']}：{e.message}")
                continue
            for item in (data.get("info_list") or [])[:MAX_RESULTS_PER_BASE]:
                if not isinstance(item, dict) or not item.get("media_id"):
                    continue  # 没有 media_id 的是文件夹
                items.append({
                    "base_id": base["id"], "base_name": base["name"], "media_id": str(item["media_id"]),
                    "title": str(item.get("title") or "（没有标题）"),
                    "snippet": clean_snippet(item.get("highlight_content")),
                })
        return {"query": query, "items": items, "errors": errors}

    # ---------------- 给 AI 的参考材料 ----------------
    def context_for(self, prompt: str, name_of: Callable[[str], str | None]) -> str:
        """如果这段提示词是针对某一只股票的，就到知识库里搜这只股票，拼成一段参考材料；否则返回空。

        只在提示词里恰好出现一只股票的代码时才搜（复盘这类一次提到很多股票的不搜）。
        搜不到、没配置、接口出错都返回空，不影响原来的 AI 调用。
        """
        config = self.config()
        if not (config["configured"] and config["use_in_ai"] and config["selected"]):
            return ""
        symbols = {f"{digits}.{exchange}" for digits, exchange in _SYMBOL.findall(prompt)}
        if len(symbols) != 1:
            return ""
        symbol = next(iter(symbols))
        query = (name_of(symbol) or "").strip() or symbol[:6]
        cached = self._context_cache.get(query)
        if cached is not None and time.monotonic() - cached[0] < CONTEXT_CACHE_SECONDS:
            items = cached[1]
        else:
            try:
                items = self.search(query)["items"]
            except ImaError as e:
                logger.info("给 AI 准备知识库参考材料没有成功：%s", e.message)
                return ""
            self._context_cache[query] = (time.monotonic(), items)
        items = [i for i in items if i["snippet"]][:CONTEXT_ITEMS]
        if not items:
            return ""
        lines = [f"{n}. 《{i['title']}》（{i['base_name']}）：{i['snippet']}" for n, i in enumerate(items, 1)]
        return (
            f"\n\n【我的知识库里和“{query}”相关的内容（来自腾讯 ima，只是搜索命中的片段，可能不完整、也可能已经过时）】\n"
            + "\n".join(lines)
            + "\n请把上面的内容当作补充背景：和行情数据有出入时以行情数据为准；用到其中的内容时说明出自知识库。"
        )

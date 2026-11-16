"""腾讯 ima 知识库：用官方开放接口在选定的知识库里搜索。

接口说明来自 ima 官方技能包（ima-skills 1.1.2）里的 ``knowledge-base/references/api.md``：

- 地址 ``https://ima.qq.com/openapi/wiki/v1/<接口名>``，全部是 POST + JSON；
- 请求头 ``ima-openapi-clientid`` 和 ``ima-openapi-apikey``（在 https://ima.qq.com/agent-interface 申请）；
- 文档写的返回是 ``{"retcode": 0, "errmsg": "...", "data": {...}}``，实测是 ``{"code": 0, "msg": "...", "data": {...}}``，
  两种都认；不是 0 时那句话就是给人看的原因；
- 这里只用到三个只读接口：``search_knowledge_base``（列出或按名称找知识库，自己的和加入的共享知识库都有）、
  ``get_addable_knowledge_base_list``（只在验证凭证时调一次）、``search_knowledge``（在一个知识库里搜索，
  返回标题和命中的片段）。

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
BASE_PAGE_SIZE = 20  # search_knowledge_base 一页最多 20 个（实测；超过会被拒绝）
BASE_PAGES = 5
NETWORK_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 1.5
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
                for attempt in range(NETWORK_ATTEMPTS):  # 连接偶尔会断一下（实测过），断了隔一会儿再试一次
                    try:
                        status, text = _post(f"{BASE_URL}/{name}", headers, body, TIMEOUT_SECONDS)
                        break
                    except Exception as e:  # 网络层的任何失败都转成一句话
                        if attempt + 1 >= NETWORK_ATTEMPTS:
                            raise ImaError(f"连不上 ima：{type(e).__name__}") from e
                        _sleep(RETRY_WAIT_SECONDS)
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
        # 官方文档写的是 retcode / errmsg，实际接口返回的是 code / msg；两种都认
        code = payload["retcode"] if "retcode" in payload else payload.get("code")
        if code != 0:
            message = str(payload.get("errmsg") or payload.get("msg") or f"错误码 {code}")
            if code == 110021:
                message = "ima 提示请求太频繁，请过一会儿再试。"
            raise ImaError(f"ima：{message}", 400 if code in (110001, 110030) else 502)
        result = payload.get("data")
        return result if isinstance(result, dict) else {}

    def knowledge_bases(self, query: str = "") -> list[dict[str, Any]]:
        """可以选的知识库：自己的、加入的共享知识库都在里面。不填名称时列出全部，填了按名称找。

        实测 ``search_knowledge_base`` 一页最多 20 个（文档写的是 50），返回的字段是 kb_id / kb_name
        （文档写的是 id / name），还带知识库的类型和篇数。这里最多翻 5 页。
        """
        found: dict[str, dict[str, Any]] = {}
        cursor = ""
        for _ in range(BASE_PAGES):
            data = self._call("search_knowledge_base", {"query": query.strip(), "cursor": cursor, "limit": BASE_PAGE_SIZE})
            rows = data.get("info_list") or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("kb_id") or item.get("id") or "")
                if not ident:
                    continue
                try:
                    count: int | None = int(item.get("content_count"))
                except (TypeError, ValueError):
                    count = None
                found[ident] = {
                    "id": ident, "name": str(item.get("kb_name") or item.get("name") or ident),
                    "kind": str(item.get("base_type") or ""), "count": count,
                }
            cursor = str(data.get("next_cursor") or "")
            if data.get("is_end") or not rows or not cursor:
                break
        return list(found.values())

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

    def search_many(self, queries: list[str]) -> dict[str, Any]:
        """依次按几个词搜索，结果合在一起（同一篇只留第一次搜到的），每条记下是按哪个词搜到的。"""
        terms = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))
        if not terms:
            raise ImaError("请填要搜索的内容。", 400)
        items: list[dict[str, str]] = []
        errors: list[str] = []
        seen: set[tuple[str, str]] = set()
        for term in terms:
            found = self.search(term)
            errors += [e for e in found["errors"] if e not in errors]
            for item in found["items"]:
                key = (item["base_id"], item["media_id"])
                if key not in seen:
                    seen.add(key)
                    items.append({**item, "matched": term})
        return {"query": "、".join(terms), "items": items, "errors": errors}

    # ---------------- 给 AI 的参考材料 ----------------
    def context_for(self, prompt: str, terms_of: Callable[[str], list[str]]) -> str:
        """如果这段提示词是针对某一只股票的，就到知识库里搜这只股票（名称、所属行业），拼成一段参考材料；否则返回空。

        只在提示词里恰好出现一只股票的代码时才搜（复盘这类一次提到很多股票的不搜）。
        搜不到、没配置、接口出错都返回空，不影响原来的 AI 调用。

        实测 ima 的开放接口基本只按标题匹配，多数结果没有正文片段。所以给 AI 的主要是“知识库里有哪些相关资料”
        的标题清单，并明确告诉它没有看到内容、不能据此下结论。
        """
        config = self.config()
        if not (config["configured"] and config["use_in_ai"] and config["selected"]):
            return ""
        symbols = {f"{digits}.{exchange}" for digits, exchange in _SYMBOL.findall(prompt)}
        if len(symbols) != 1:
            return ""
        symbol = next(iter(symbols))
        terms = [t for t in terms_of(symbol) if t] or [symbol[:6]]
        key = "、".join(terms)
        cached = self._context_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < CONTEXT_CACHE_SECONDS:
            items = cached[1]
        else:
            try:
                items = self.search_many(terms)["items"]
            except ImaError as e:
                logger.info("给 AI 准备知识库参考材料没有成功：%s", e.message)
                return ""
            self._context_cache[key] = (time.monotonic(), items)
        items = sorted(items, key=lambda i: not i["snippet"])[:CONTEXT_ITEMS]  # 有片段的排前面
        if not items:
            return ""
        lines = []
        for n, i in enumerate(items, 1):
            body = f"：{i['snippet']}" if i["snippet"] else "（只有标题，没有内容）"
            lines.append(f"{n}. 《{i['title']}》（{i['base_name']}，按“{i['matched']}”搜到）{body}")
        return (
            f"\n\n【我的知识库（腾讯 ima）里和“{key}”相关的资料】\n"
            + "\n".join(lines)
            + "\n说明：上面多数只有标题，你没有看到这些资料的正文，不要根据标题推断里面的结论或数据。"
              "有正文片段的可以作为补充背景，和行情数据有出入时以行情数据为准，用到时说明出自知识库。"
              "请在报告最后加一小节“知识库里的相关资料”，把这些标题原样列出来，方便我自己去看。"
        )

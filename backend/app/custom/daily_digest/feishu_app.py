"""用飞书“自建应用”的机器人把网页文件发进群。

群里的“自定义机器人”（Webhook）只能发文字和卡片，发不了文件；发文件要用自建应用：
先换一个临时令牌，再上传文件，最后把文件发到群里。一次发送共 3 个请求，都只发往飞书开放平台。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 20.0

# (方法, 地址, 其余参数) → 飞书返回的 JSON。测试里换成假的。
Http = Callable[..., dict[str, Any]]


class FeishuError(Exception):
    """飞书没有办成，message 是给人看的原因。"""


def _http(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    import httpx

    try:
        response = httpx.request(method, url, timeout=TIMEOUT, **kwargs)
        data = response.json()
    except httpx.HTTPError as e:
        raise FeishuError(f"连不上飞书：{e}") from e
    except ValueError as e:
        raise FeishuError(f"飞书返回的内容看不懂（HTTP {response.status_code}）") from e
    return data if isinstance(data, dict) else {}


def _checked(data: dict[str, Any], doing: str) -> dict[str, Any]:
    if data.get("code") != 0:
        raise FeishuError(f"{doing}没有成功：{data.get('msg') or '未知原因'}（飞书错误码 {data.get('code')}）")
    return data


def token(app_id: str, app_secret: str, http: Http = _http) -> str:
    data = _checked(http("POST", f"{BASE}/auth/v3/tenant_access_token/internal",
                         json={"app_id": app_id, "app_secret": app_secret}), "用应用 ID 和密钥登录")
    return str(data["tenant_access_token"])


def chats(access: str, http: Http = _http) -> list[dict[str, str]]:
    """这个机器人所在的群：[{chat_id, name}]。"""
    data = _checked(http("GET", f"{BASE}/im/v1/chats", params={"page_size": 100},
                         headers={"Authorization": f"Bearer {access}"}), "读取机器人所在的群")
    return [{"chat_id": str(c.get("chat_id")), "name": str(c.get("name") or "")}
            for c in (data.get("data") or {}).get("items") or [] if c.get("chat_id")]


def send_file(access: str, chat_id: str, name: str, content: bytes, http: Http = _http) -> None:
    headers = {"Authorization": f"Bearer {access}"}
    uploaded = _checked(http("POST", f"{BASE}/im/v1/files", headers=headers,
                             data={"file_type": "stream", "file_name": name},
                             files={"file": (name, content, "text/html")}), "上传文件")
    file_key = (uploaded.get("data") or {}).get("file_key")
    if not file_key:
        raise FeishuError("上传文件后飞书没有给出文件编号")
    _checked(http("POST", f"{BASE}/im/v1/messages", params={"receive_id_type": "chat_id"}, headers=headers,
                  json={"receive_id": chat_id, "msg_type": "file", "content": json.dumps({"file_key": file_key})}),
             "把文件发到群里")


def pick_chat(found: list[dict[str, str]], wanted: str) -> dict[str, str]:
    """要发到哪个群。指定了就用指定的；没指定时机器人只在一个群里就用那个。"""
    if wanted:
        for chat in found:
            if chat["chat_id"] == wanted:
                return chat
        raise FeishuError("机器人已经不在之前选的那个群里了，请重新选择群。")
    if not found:
        raise FeishuError("机器人还没有加入任何群。请先在飞书群的“设置 → 群机器人”里把这个应用的机器人加进去。")
    if len(found) > 1:
        raise FeishuError("机器人在多个群里，请在设置里选一个要接收的群。")
    return found[0]


def deliver(app_id: str, app_secret: str, chat_id: str, name: str, content: bytes, http: Http = _http) -> dict[str, str]:
    """登录、确定群、发文件。返回实际发到的群。"""
    access = token(app_id, app_secret, http)
    chat = pick_chat(chats(access, http), chat_id)
    send_file(access, chat["chat_id"], name, content, http)
    return chat

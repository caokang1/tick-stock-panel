from __future__ import annotations

import hashlib
import hmac
import json
import logging

from app.services import email_adapter, webhook_adapter


def test_custom_webhook_posts_stable_signed_json(monkeypatch):
    captured = {}

    class Response:
        status_code = 204
        text = ""

    def fake_post(url, *, content, headers, timeout):
        captured.update(url=url, content=content, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(webhook_adapter.time, "time", lambda: 1_700_000_000)

    assert webhook_adapter.send_custom(
        "https://example.com/tickflow",
        "价格预警",
        "600000.SH 触发",
        "monitor_alert",
        {"symbol": "600000.SH"},
        "shared-secret",
    )
    payload = json.loads(captured["content"])
    assert payload == {
        "event": "monitor_alert",
        "timestamp": 1_700_000_000,
        "title": "价格预警",
        "body": "600000.SH 触发",
        "data": {"symbol": "600000.SH"},
    }
    expected = hmac.new(b"shared-secret", captured["content"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-TickFlow-Signature"] == f"sha256={expected}"
    assert captured["headers"]["X-TickFlow-Timestamp"] == "1700000000"


def test_custom_webhook_rejects_non_http_urls(monkeypatch):
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    assert not webhook_adapter.send_custom("file:///tmp/hook", "title", "body", "test")


def test_email_adapter_uses_starttls_login_and_multiple_recipients(monkeypatch):
    instances = []

    class FakeSmtp:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.calls = []
            instances.append(self)

        def ehlo(self):
            self.calls.append(("ehlo",))

        def starttls(self):
            self.calls.append(("starttls",))

        def login(self, username, password):
            self.calls.append(("login", username, password))

        def send_message(self, message):
            self.calls.append(("send", message))

        def quit(self):
            self.calls.append(("quit",))

        def close(self):
            self.calls.append(("close",))

    monkeypatch.setattr(email_adapter.smtplib, "SMTP", FakeSmtp)
    config = {
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "bot@example.com",
        "from_address": "bot@example.com",
        "to_addresses": ["one@example.com", "two@example.com"],
    }

    assert email_adapter.send_email(config, "smtp-password", "监控告警", "正文")
    smtp = instances[0]
    assert smtp.calls[:4] == [
        ("ehlo",),
        ("starttls",),
        ("ehlo",),
        ("login", "bot@example.com", "smtp-password"),
    ]
    message = next(call[1] for call in smtp.calls if call[0] == "send")
    assert message["To"] == "one@example.com, two@example.com"
    assert message["Subject"] == "监控告警"
    assert smtp.calls[-1] == ("quit",)


def _unreachable(*_args, **_kwargs):
    raise ConnectionError("unreachable")


def test_feishu_single_attempt_returns_without_backoff_and_logs_the_real_count(monkeypatch, caplog):
    # 设置页「发送测试消息」传 max_attempts=1: 失败即返回, 不等退避, 日志计数如实。
    sleeps: list[float] = []
    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("httpx.post", _unreachable)
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        ok = webhook_adapter.send_feishu(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "标题", "正文", max_attempts=1
        )
    assert ok is False
    assert sleeps == []
    assert "已重试 1 次" in caplog.text


def test_feishu_production_retries_keep_backoff(monkeypatch, caplog):
    # 生产路径 (默认 3 次) 的退避与日志不变: 1s、2s 两次退避, 日志写 3 次。
    sleeps: list[float] = []
    calls = {"n": 0}

    def unreachable(*_args, **_kwargs):
        calls["n"] += 1
        raise ConnectionError("unreachable")

    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("httpx.post", unreachable)
    with caplog.at_level(logging.WARNING, logger=webhook_adapter.__name__):
        ok = webhook_adapter.send_feishu(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc", "标题", "正文"
        )
    assert ok is False
    assert calls["n"] == 3
    assert sleeps == [1, 2]
    assert "已重试 3 次" in caplog.text


def test_email_adapter_finds_http_proxy_and_ignores_others(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert email_adapter.http_proxy() is None
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:7890")
    assert email_adapter.http_proxy() is None
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert email_adapter.http_proxy() == ("127.0.0.1", 7890)


def test_email_adapter_sends_html_body_and_attachment(monkeypatch):
    sent = []

    class FakeSsl:
        def __init__(self, host, port, timeout):
            pass

        def login(self, username, password):
            pass

        def send_message(self, message):
            sent.append(message)

        def quit(self):
            pass

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(email_adapter.smtplib, "SMTP_SSL", FakeSsl)
    config = {"host": "smtp.example.com", "port": 465, "security": "ssl", "username": "a@example.com",
              "from_address": "a@example.com", "to_addresses": ["b@example.com"]}
    assert email_adapter.send_email(config, "pw", "标题", "纯文字", html="<p>网页</p>",
                                    attachments=[("汇总.html", "<p>网页</p>".encode(), "text/html")])
    parts = {part.get_content_type(): part for part in sent[0].walk() if not part.is_multipart()}
    assert "纯文字" in sent[0].get_body(("plain",)).get_content()
    assert "<p>网页</p>" in sent[0].get_body(("html",)).get_content()
    attached = [part for part in sent[0].iter_attachments()]
    assert [a.get_filename() for a in attached] == ["汇总.html"] and "text/html" in parts

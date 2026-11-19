"""SMTP email notification adapter.

Transport failures are isolated from alert persistence and SSE delivery. SMTP credentials
are supplied by the caller from ``secrets_store`` and never logged here.
"""
from __future__ import annotations

import logging
import os
import smtplib
import socket
import time
from contextlib import suppress
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SECURITY_MODES = {"ssl", "starttls", "none"}
_MAX_ATTEMPTS = 2


def is_valid_email(address: str) -> bool:
    """Small dependency-free mailbox validation suitable for configuration checks."""
    parsed = parseaddr((address or "").strip())[1]
    if parsed != (address or "").strip() or parsed.count("@") != 1:
        return False
    local, domain = parsed.rsplit("@", 1)
    return bool(local and domain and "." in domain and " " not in parsed)


def is_configured(config: dict) -> bool:
    """Return whether the non-secret fields are sufficient to attempt delivery."""
    sender = str(config.get("from_address") or config.get("username") or "").strip()
    recipients = config.get("to_addresses") or []
    return bool(config.get("host") and sender and recipients)


def http_proxy() -> tuple[str, int] | None:
    """本机设置的 HTTP 代理 (HTTPS_PROXY / HTTP_PROXY 环境变量); 没设或不是 http 代理返回 None。

    本地定制: 只能经代理上网的电脑上, smtplib 直连邮件服务器会一直超时, 邮件发不出去。
    设了代理时改为经代理的 CONNECT 隧道连接邮件服务器 (TLS 仍然是和邮件服务器之间端到端的)。
    """
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if parsed.scheme == "http" and parsed.hostname and parsed.port:
            return parsed.hostname, parsed.port
    return None


def _tunnel(proxy: tuple[str, int], host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection(proxy, timeout)
    try:
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"))
        reply = b""
        while b"\r\n\r\n" not in reply and len(reply) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                break
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode("latin-1")
        if " 200" not in status:
            raise OSError(f"代理没有接通邮件服务器: {status or '无应答'}")
        return sock
    except BaseException:
        sock.close()
        raise


def _via_proxy(base: type, proxy: tuple[str, int], use_ssl: bool) -> type:
    """base (smtplib.SMTP / SMTP_SSL) 的子类: 建连接时走代理隧道, 其余行为不变。"""
    def _get_socket(self, host, port, timeout):
        sock = _tunnel(proxy, host, port, timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host) if use_ssl else sock

    return type(f"Proxied{base.__name__}", (base,), {"_get_socket": _get_socket})


def send_email(
    config: dict,
    password: str,
    subject: str,
    body: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    """Send one UTF-8 email through SSL, STARTTLS, or plain SMTP.

    body 是纯文本正文; html 不为空时作为网页版正文一并带上 (收信端优先显示网页版)。
    attachments: [(文件名, 内容, MIME 类型如 "text/html")]。
    """
    if not is_configured(config):
        return False

    host = str(config.get("host") or "").strip()
    try:
        port = int(config.get("port", 465))
    except (TypeError, ValueError):
        return False
    security = str(config.get("security") or "ssl")
    username = str(config.get("username") or "").strip()
    sender = str(config.get("from_address") or username).strip()
    recipients = [str(item).strip() for item in config.get("to_addresses", [])]
    if (
        not 1 <= port <= 65535
        or security not in SECURITY_MODES
        or not is_valid_email(sender)
        or not recipients
        or any(not is_valid_email(item) for item in recipients)
    ):
        return False

    message = EmailMessage()
    message["Subject"] = str(subject or "TickFlow 通知")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(str(body or ""))
    if html:
        message.add_alternative(html, subtype="html")
    for name, content, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(content, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)

    proxy = http_proxy()
    ssl_class = _via_proxy(smtplib.SMTP_SSL, proxy, True) if proxy else smtplib.SMTP_SSL
    plain_class = _via_proxy(smtplib.SMTP, proxy, False) if proxy else smtplib.SMTP

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        smtp = None
        try:
            if security == "ssl":
                smtp = ssl_class(host, port, timeout=10)
            else:
                smtp = plain_class(host, port, timeout=10)
                if security == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
            # Delivery already succeeded; a failed QUIT must not retry and duplicate the email.
            try:
                smtp.quit()
            except Exception:
                with suppress(Exception):
                    smtp.close()
            return True
        except Exception as exc:  # SMTP/network errors must not escape
            last_err = str(exc)
            if smtp is not None:
                with suppress(Exception):
                    smtp.close()
        if attempt < max_attempts:
            time.sleep(1)

    logger.warning("邮件推送最终失败(已尝试 %d 次): %s", max_attempts, last_err)
    return False

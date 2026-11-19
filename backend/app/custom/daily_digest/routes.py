"""每日汇总的接口和每分钟检查一次的后台线程。"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import feishu_app, render, shots, snapshot

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/daily-digest"
TICK_SECONDS = 60
MARKET_CLOSE = time(15, 0)
WAIT_FOR_RECAP = timedelta(minutes=45)  # 复盘到点后最多等这么久；还没有就不等了，只发系统快照
AFTER_PIPELINE = timedelta(minutes=10)  # 没开定时复盘时：盘后同步的时间再过这么久
MAX_ATTEMPTS = 3  # 飞书没发出去时，同一天最多试几次
KEEP_PAGES = 90
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_CONFIG = {"enabled": True, "feishu_card": True, "base_url": "http://localhost:3018",
                  "screenshots": True, "panel_url": "http://127.0.0.1:3011", "browser": "", "app_id": "", "chat_id": "", "chat_name": ""}
SECRET_KEY = "daily_digest_feishu_app_secret"  # 应用密钥放在面板的 secrets.json 里，不进设置文件

Send = Callable[[str, str, str], bool]  # (标题, 副标题, 正文) → 是否发出去了
SendFile = Callable[[str, bytes], str | None]  # (文件名, 内容) → 没发出去的原因；None 是发出去了或者没配置
SendMail = Callable[[str, str, str], bool | None]  # (标题, 纯文字正文, 网页) → 发出去没有；None 是没配置邮箱


class OptionsIn(BaseModel):
    feishu_card: bool


class AppIn(BaseModel):
    app_id: str
    app_secret: str | None = None  # 不传或传空：沿用已经存着的
    chat_id: str = ""


def root_of(data_dir: Path) -> Path:
    return data_dir / "user_data" / "extensions" / "daily_digest"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(root: Path) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **_read_json(root / "config.json")}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def due(now: datetime, state: dict[str, Any], latest: date | None, recap_done: bool, job_running: bool,
        review: dict[str, Any], pipeline: dict[str, Any]) -> str | None:
    """现在该不该发今天的汇总。"ready"：都跑完了；"no_recap"：复盘等不到了，只发快照；None：还不到时候。

    latest：面板里行情的最新日期。它不是今天，说明今天没开市或者行情还没同步进来，不发。
    """
    today = now.date()
    if state.get("sent_for") == today.isoformat() or latest != today or job_running:
        return None
    if int(state.get("attempts", 0)) >= MAX_ATTEMPTS and state.get("attempt_day") == today.isoformat():
        return None
    if review.get("enabled"):
        start = datetime.combine(today, time(int(review.get("hour", 15)), int(review.get("minute", 40))))
        if now < start:
            return None
        if recap_done:
            return "ready"
        return "no_recap" if now >= start + WAIT_FOR_RECAP else None
    start = datetime.combine(today, time(int(pipeline.get("hour", 15)), int(pipeline.get("minute", 35)))) + AFTER_PIPELINE
    return "ready" if now >= max(start, datetime.combine(today, MARKET_CLOSE)) else None


def save_page(root: Path, day: str, page: str) -> Path:
    pages = root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    path = pages / f"{day}.html"
    path.write_text(page, encoding="utf-8")
    for old in sorted(pages.glob("*.html"))[:-KEEP_PAGES]:
        old.unlink(missing_ok=True)
    return path


def page_days(root: Path) -> list[str]:
    return sorted((p.stem for p in (root / "pages").glob("*.html") if DAY.match(p.stem)), reverse=True)


Shoot = Callable[[Path], list[dict[str, Any]]]  # 给面板各页面截图：[{"title", "image" 或 "error"}]


def take_shots(root: Path) -> list[dict[str, Any]]:
    """按设置给面板的几个页面截长图。关掉了、找不到浏览器、浏览器起不来：返回空，网页退回文字版。"""
    config = load_config(root)
    if not config["screenshots"]:
        return []
    browser = shots.find_browser(str(config["browser"]))
    if not browser:
        logger.warning("每日汇总：这台电脑上没有找到 Chrome/Chromium，网页用文字版")
        return []
    try:
        return shots.capture(str(config["panel_url"]), browser, work=root / "browser")
    except Exception:
        logger.exception("每日汇总：截图没有成功，网页用文字版")
        return []


def publish(root: Path, digest: dict[str, Any], send: Send | None, send_file: SendFile | None = None,
            send_mail: SendMail | None = None, shoot: Shoot | None = None) -> dict[str, Any]:
    """存成网页；send 不为空时把卡片推到飞书，并（配置了的话）把网页发到邮箱、作为文件发进飞书群。

    卡片最后发：邮件和文件发没发出去都写在卡片里。
    返回 {"day", "link", "sent", "file_error", "mailed"}；mailed 为 None 表示没配置邮箱。
    """
    day = str(digest["day"])
    taken = shoot(root) if shoot is not None else []
    good = [s["title"] for s in taken if s.get("image")]
    page = render.shots_page(digest, taken) if good else render.page(digest)
    save_page(root, day, page)
    link = f'{str(load_config(root)["base_url"]).rstrip("/")}{PREFIX}/page/{day}'
    sent = file_error = mailed = None
    if send_mail is not None:
        mailed = send_mail(f"{day} 每日汇总", render.brief(digest, good), page)
    if send is not None:
        with_file = False
        if send_file is not None and app_ready(root):
            file_error = send_file(f"{day} 每日汇总.html", page.encode("utf-8"))
            with_file = file_error is None
        subtitle, body = render.card(digest, link, with_file=with_file, file_error=file_error, mailed=mailed)
        sent = bool(send("每日汇总", subtitle, body))
    return {"day": day, "link": link, "sent": sent, "file_error": file_error, "mailed": mailed,
            "shots": len(good), "shots_failed": [s["title"] for s in taken if not s.get("image")]}


def mail_ready() -> bool:
    from app import secrets_store
    from app.services import email_adapter, preferences

    return email_adapter.is_configured(preferences.get_email_smtp_config()) and bool(secrets_store.get_email_smtp_password())


def mail_send(subject: str, text: str, page: str) -> bool | None:
    """用面板“实时监控”里配好的邮箱发：网页直接放在正文里，同时作为附件带一份。没配邮箱返回 None。"""
    from app import secrets_store
    from app.services import email_adapter, preferences

    if not mail_ready():
        return None
    return email_adapter.send_email(
        preferences.get_email_smtp_config(), secrets_store.get_email_smtp_password(), subject, text,
        html=render.brief_html(text), attachments=[(f"{subject}.html", page.encode("utf-8"), "text/html")],
    )


def app_ready(root: Path) -> bool:
    from app import secrets_store

    return bool(load_config(root)["app_id"] and secrets_store.load().get(SECRET_KEY))


def file_sender(root: Path, http: feishu_app.Http | None = None) -> SendFile:
    """用设置里的自建应用把文件发进群；群没选过、而机器人只在一个群里时，把那个群记下来。"""
    def send(name: str, content: bytes) -> str | None:
        from app import secrets_store

        config = load_config(root)
        try:
            args = (config["app_id"], str(secrets_store.load().get(SECRET_KEY) or ""), config["chat_id"], name, content)
            chat = feishu_app.deliver(*args, http) if http else feishu_app.deliver(*args)
        except feishu_app.FeishuError as e:
            logger.warning("每日汇总：网页文件没有发到飞书：%s", e)
            return str(e)
        if chat["chat_id"] != config["chat_id"] or chat["name"] != config["chat_name"]:
            _write_json(root / "config.json", {**_read_json(root / "config.json"),
                                               "chat_id": chat["chat_id"], "chat_name": chat["name"]})
        return None

    return send


def feishu_send(title: str, subtitle: str, body: str) -> bool:
    """用面板“监控中心”里配好的那个飞书机器人发一张卡片。没配就不发。"""
    from app.services import preferences, webhook_adapter

    url = preferences.get_feishu_webhook_url()
    if not url:
        logger.info("每日汇总：还没有配置飞书机器人，没有推送")
        return False
    return webhook_adapter.send_feishu_card(url, title, subtitle, body, preferences.get_feishu_webhook_secret())


def channels(root: Path, send: Send, send_file: SendFile | None, send_mail: SendMail) -> tuple[Send | None, SendFile | None, SendMail]:
    """按设置决定往哪里发：关掉了“飞书摘要卡片”时，飞书那边什么都不发，只发邮件。"""
    if load_config(root)["feishu_card"]:
        return send, send_file or file_sender(root), send_mail
    return None, None, send_mail


def delivered(result: dict[str, Any]) -> bool:
    """这一次算不算发出去了：开着的渠道里至少有一个成功。"""
    return bool(result["sent"]) or bool(result["mailed"])


def tick(app: Any, now: datetime, send: Send = feishu_send, send_file: SendFile | None = None,
         send_mail: SendMail = mail_send, shoot: Shoot = take_shots) -> str | None:
    """每分钟看一眼。发了（或者试了）就返回一句说明，否则 None。"""
    from app.custom.recap_plus.sync import already_done
    from app.services import market_recap_reports, preferences
    from app.services.pipeline_jobs import job_store

    repo = getattr(app.state, "repo", None)
    raw = getattr(getattr(repo, "store", None), "data_dir", None)
    if not isinstance(raw, (str, Path)):
        return None
    root = root_of(Path(raw))
    if not load_config(root)["enabled"] or now.time() < MARKET_CLOSE:
        return None
    state = _read_json(root / "state.json")
    today = now.date()
    if state.get("sent_for") == today.isoformat():
        return None
    latest = repo.latest_enriched_date()
    reason = due(
        now, state, latest,
        recap_done=already_done(market_recap_reports.list_reports(), latest),
        job_running=job_store.active_id() is not None,
        review=preferences.get_review_schedule(), pipeline=preferences.get_pipeline_schedule(),
    )
    if reason is None:
        return None
    result = publish(root, snapshot.collect(app.state, today, now), *channels(root, send, send_file, send_mail), shoot)
    attempts = int(state.get("attempts", 0)) + 1 if state.get("attempt_day") == today.isoformat() else 1
    if delivered(result):
        parts = (["飞书卡片"] if result["sent"] else []) + (["邮件"] if result["mailed"] else [])
        message = f"{now:%H:%M} 已发出（{'、'.join(parts)}）" + ("，今天的复盘没有生成，只有系统快照" if reason == "no_recap" else "")
        if result["sent"] is False:
            message += "；飞书卡片没有发出去"
        if result["file_error"]:
            message += f"；网页文件没有发进飞书：{result['file_error']}"
        if result["mailed"] is False:
            message += "；邮件没有发出去"
        if result["shots_failed"]:
            message += f"；没截下来的页面：{'、'.join(result['shots_failed'])}"
        state = {"sent_for": today.isoformat(), "message": message}
    else:
        message = f"{now:%H:%M} 网页已生成，但没有发出去（第 {attempts} 次）"
        state = {**state, "attempt_day": today.isoformat(), "attempts": attempts, "message": message}
    _write_json(root / "state.json", state)
    logger.info("每日汇总 %s：%s", today, message)
    return message


def build_router(background: bool = True) -> APIRouter:
    @asynccontextmanager
    async def lifespan(app: Any):
        stopping = threading.Event()
        thread = None
        if background:
            def loop() -> None:
                while not stopping.wait(TICK_SECONDS):
                    try:
                        tick(app, datetime.now())
                    except Exception:
                        logger.exception("每日汇总的定时检查出错")

            thread = threading.Thread(target=loop, name="daily-digest", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stopping.set()
            if thread is not None:
                thread.join(timeout=5)

    router = APIRouter(prefix=PREFIX, tags=["custom-daily-digest"], lifespan=lifespan)

    def root(request: Request) -> Path:
        return root_of(Path(request.app.state.repo.store.data_dir))

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        from app import secrets_store

        r = root(request)
        return {**load_config(r), "secret_set": bool(secrets_store.load().get(SECRET_KEY)), "mail_ready": mail_ready(),
                "state": _read_json(r / "state.json"), "pages": page_days(r)[:30]}

    @router.put("/app")
    def save_app(body: AppIn, request: Request) -> dict[str, Any]:
        """保存自建应用的 ID 和密钥，并马上用它们登录一次、列出机器人所在的群，确认填得对。"""
        from app import secrets_store

        r = root(request)
        app_id = body.app_id.strip()
        secret = (body.app_secret or "").strip() or str(secrets_store.load().get(SECRET_KEY) or "")
        if not app_id or not secret:
            raise HTTPException(status_code=400, detail="应用 ID 和应用密钥都要填。")
        try:
            found = feishu_app.chats(feishu_app.token(app_id, secret))
        except feishu_app.FeishuError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        chosen = next((c for c in found if c["chat_id"] == body.chat_id), None) or (found[0] if len(found) == 1 else None)
        secrets_store.save({SECRET_KEY: secret})
        _write_json(r / "config.json", {**_read_json(r / "config.json"), "app_id": app_id,
                                        "chat_id": chosen["chat_id"] if chosen else "",
                                        "chat_name": chosen["name"] if chosen else ""})
        return {**status(request), "chats": found}

    @router.delete("/app")
    def remove_app(request: Request) -> dict[str, Any]:
        from app import secrets_store

        r = root(request)
        secrets_store.clear(SECRET_KEY)
        _write_json(r / "config.json", {**_read_json(r / "config.json"), "app_id": "", "chat_id": "", "chat_name": ""})
        return status(request)

    @router.get("/page/{day}", response_class=HTMLResponse)
    def page(day: str, request: Request) -> HTMLResponse:
        r = root(request)
        if day == "latest":
            days = page_days(r)
            day = days[0] if days else ""
        if not DAY.match(day) or not (r / "pages" / f"{day}.html").is_file():
            raise HTTPException(status_code=404, detail="这一天没有生成过每日汇总。")
        return HTMLResponse((r / "pages" / f"{day}.html").read_text(encoding="utf-8"))

    @router.post("/run")
    def run(request: Request, send: bool = False) -> dict[str, Any]:
        """现在就按面板里最新一天的行情生成一份；send=true 时同时推到飞书。不改“今天发过没有”的记录。"""
        latest = request.app.state.repo.latest_enriched_date()
        if latest is None:
            raise HTTPException(status_code=409, detail="面板里还没有行情，生成不了。")
        digest = snapshot.collect(request.app.state, latest, datetime.now())
        if not send:
            return publish(root(request), digest, None, shoot=take_shots)
        return publish(root(request), digest, *channels(root(request), feishu_send, None, mail_send), take_shots)

    @router.put("/options")
    def options(body: OptionsIn, request: Request) -> dict[str, Any]:
        r = root(request)
        _write_json(r / "config.json", {**_read_json(r / "config.json"), "feishu_card": body.feishu_card})
        return status(request)

    return router

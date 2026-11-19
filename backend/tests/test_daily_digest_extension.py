"""“每日汇总”扩展的测试。数据都是造的，网页写在临时目录里，飞书用假的发送函数顶替，不联网。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.custom.daily_digest import (
    EXTENSION_API_VERSION,
    EXTENSION_ID,
    feishu_app,
    render,
    routes,
    snapshot,
)
from app.extensions import BACKEND_EXTENSION_API_VERSION

TODAY = date(2026, 11, 19)
REVIEW = {"enabled": True, "hour": 18, "minute": 5}
PIPELINE = {"hour": 17, "minute": 55}


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 19, hour, minute)


def digest(recap: bool = True) -> dict:
    return {
        "day": "2026-11-19", "generated_at": "2026-11-19 18:10",
        "tasks": [{"name": "收盘行情", "ok": True, "text": "已更新到 2026-11-19"},
                  {"name": "每日复盘", "ok": recap, "text": "已生成" if recap else "今天的复盘没有生成"}],
        "market": {
            "indices": [{"symbol": "000001.SH", "name": "上证指数", "last": 3577.62, "pct": -0.0028}],
            "breadth": {"up": 637, "down": 4533, "flat": 47, "median_pct": -0.0245},
            "amount_total": 1.3e12, "limit_up": 28, "limit_down": 83, "max_boards": 3,
            "emotion_score": 23.0, "emotion_label": "冰点",
            "cap": [{"symbol": "601398.SH", "name": "工商银行", "market_cap": 3.24e12, "change_pct": 0.0044}],
            "industry": [{"name": "半导体", "amount": 2.15e11, "share_pct": 16.5, "avg_pct": -0.0069}],
            "turnover": [], "active": [],
        },
        "holdings": snapshot.holdings_of(
            [{"symbol": "002832.SZ", "qty": 100, "cost": 20.0}, {"symbol": "510300.SH", "qty": 100, "cost": -50.0},
             {"symbol": "00981.HK", "qty": 500, "cost": 70.0}, {"symbol": "000001.SZ", "qty": 100, "cost": 10.0}],
            {"002832.SZ": {"name": "比音勒芬", "price": 30.0, "change_pct": 0.01},
             "510300.SH": {"name": None, "price": 4.0, "change_pct": 0.004}},
            {"items": {"00981.HK": {"name": "中芯国际", "last": 50.0, "change_pct": 0.03}}, "rate": 0.9},
        ),
        "alerts": [{"time": "10:52", "symbol": "002832.SZ", "name": "比音勒芬", "rule_name": "涨到+2%", "message": "<b>可以高抛</b>", "price": 29.6}],
        "recap": {"content": "### 一、市场全景\n\n| 指数 | 涨跌 |\n|---|---|\n| 上证 | -0.28% |\n\n- **缩量**下跌\n", "created_at": "2026-11-19 18:08"} if recap else None,
    }


def test_extension_identity():
    assert EXTENSION_ID == "community.daily-digest"
    assert EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_due_waits_for_all_scheduled_tasks():
    args = {"review": REVIEW, "pipeline": PIPELINE}
    assert routes.due(at(17, 0), {}, TODAY, True, False, **args) is None  # 复盘还没到点（哪怕手动跑过）
    assert routes.due(at(18, 6), {}, TODAY, False, False, **args) is None  # 复盘还在生成
    assert routes.due(at(18, 9), {}, TODAY, True, True, **args) is None  # 还有任务在跑
    assert routes.due(at(18, 9), {}, TODAY, True, False, **args) == "ready"
    assert routes.due(at(18, 49), {}, TODAY, False, False, **args) is None
    assert routes.due(at(18, 50), {}, TODAY, False, False, **args) == "no_recap"  # 等了 45 分钟还没有
    assert routes.due(at(18, 9), {}, date(2026, 11, 18), True, False, **args) is None  # 今天没有行情（休市）
    assert routes.due(at(18, 9), {"sent_for": "2026-11-19"}, TODAY, True, False, **args) is None
    tried = {"attempt_day": "2026-11-19", "attempts": routes.MAX_ATTEMPTS}
    assert routes.due(at(18, 9), tried, TODAY, True, False, **args) is None
    assert routes.due(at(18, 9), {**tried, "attempt_day": "2026-11-18"}, TODAY, True, False, **args) == "ready"


def test_due_without_scheduled_review_follows_pipeline():
    args = {"review": {"enabled": False}, "pipeline": PIPELINE}
    assert routes.due(at(18, 4), {}, TODAY, False, False, **args) is None
    assert routes.due(at(18, 5), {}, TODAY, False, False, **args) == "ready"


def test_holdings_convert_hk_and_skip_missing_prices():
    h = digest()["holdings"]
    rows = {r["symbol"]: r for r in h["rows"]}
    assert rows["002832.SZ"]["pnl"] == 1000 and round(rows["002832.SZ"]["pnl_pct"], 4) == 0.5
    assert rows["510300.SH"]["pnl"] == 5400 and rows["510300.SH"]["pnl_pct"] is None  # 负成本：只有金额
    assert rows["00981.HK"]["value"] == 500 * 50 * 0.9 and rows["00981.HK"]["pnl"] == 500 * -20 * 0.9
    assert rows["000001.SZ"]["value"] is None
    assert h["total_value"] == 3000 + 400 + 22500 and h["total_pnl"] == 1000 + 5400 - 9000
    assert "000001.SZ" in h["note"] and "0.9000" in h["note"]


def test_alerts_keep_only_that_day_in_time_order():
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    events = [{"ts": ms(datetime(2026, 11, 19, 14, 0)), "symbol": "A"}, {"ts": ms(datetime(2026, 11, 18, 10, 0)), "symbol": "B"},
              {"ts": ms(datetime(2026, 11, 19, 9, 45)), "symbol": "C"}, {"ts": "坏的"}]
    assert [(a["time"], a["symbol"]) for a in snapshot.alerts_of(events, TODAY)] == [("09:45", "C"), ("14:00", "A")]


def test_recap_of_picks_latest_report_of_the_day():
    reports = [{"as_of": "2026-11-19", "content": "早", "created_at": "2026-11-19T15:10:00"},
               {"as_of": "2026-11-19", "content": "晚", "created_at": "2026-11-19T18:08:00"},
               {"as_of": "2026-11-18", "content": "昨天", "created_at": "2026-11-18T18:08:00"}]
    assert snapshot.recap_of(reports, TODAY)["content"] == "晚"
    assert snapshot.recap_of(reports, date(2026, 11, 17)) is None


def test_markdown_renders_tables_lists_and_escapes_html():
    out = render.md_to_html("## 标题\n\n| a | b |\n|---|---|\n| 1 | <x> |\n\n- **粗** 一\n- 二\n\n1. 甲\n\n> 引用\n\n正文 `c`")
    assert "<h3>标题</h3>" in out and "<th>a</th>" in out and "<td>&lt;x&gt;</td>" in out
    assert "<ul><li><strong>粗</strong> 一</li><li>二</li></ul>" in out and "<ol><li>甲</li></ol>" in out
    assert "<blockquote>引用</blockquote>" in out and "<p>正文 <code>c</code></p>" in out


def test_page_has_every_section_and_escapes_user_text():
    page = render.page(digest())
    for text in ("2026-11-19 每日汇总", "今天的定时任务", "市场快照", "行业成交额前十", "我的持仓", "今天的监控信号（1 条）",
                 "每日复盘", "<strong>缩量</strong>", "-0.28%", "HK$50.000"):
        assert text in page
    assert "<b>可以高抛</b>" not in page and "&lt;b&gt;可以高抛&lt;/b&gt;" in page
    assert "今天的复盘没有生成，下面只有系统快照" in render.page(digest(recap=False))


def test_publish_saves_page_and_sends_card(tmp_path):
    sent = []
    result = routes.publish(tmp_path, digest(), lambda title, subtitle, body: sent.append((title, subtitle, body)) or True)
    assert result == {"day": "2026-11-19", "link": "http://localhost:3018/api/custom/daily-digest/page/2026-11-19",
                      "sent": True, "file_error": None, "mailed": None, "shots": 0, "shots_failed": []}
    assert (tmp_path / "pages" / "2026-11-19.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    title, subtitle, body = sent[0]
    assert title == "每日汇总" and subtitle == "2026-11-19 · 情绪 冰点"
    assert result["link"] in body and "上证指数 -0.28%" in body and "一、市场全景" in body and "半导体" in body
    assert routes.page_days(tmp_path) == ["2026-11-19"]
    assert routes.publish(tmp_path, digest(), None)["sent"] is None and len(sent) == 1
    assert "需要留意" in render.card(digest(recap=False), "x")[1]


def fake_feishu(calls: list, chats: list[dict], fail: str | None = None):
    def http(method: str, url: str, **kwargs):
        step = url.rsplit("/", 1)[-1]
        calls.append((method, step, kwargs))
        if step == fail:
            return {"code": 99991, "msg": "没有权限"}
        if step == "internal":
            return {"code": 0, "tenant_access_token": "t-1"}
        if step == "chats":
            return {"code": 0, "data": {"items": chats}}
        if step == "files":
            return {"code": 0, "data": {"file_key": "file_1"}}
        return {"code": 0}
    return http


def test_feishu_app_uploads_then_sends_file_to_the_only_chat():
    calls: list = []
    chat = feishu_app.deliver("cli_x", "secret", "", "a.html", b"<html>", fake_feishu(calls, [{"chat_id": "oc_1", "name": "我的群"}]))
    assert chat == {"chat_id": "oc_1", "name": "我的群"}
    assert [c[1] for c in calls] == ["internal", "chats", "files", "messages"]
    assert calls[0][2]["json"] == {"app_id": "cli_x", "app_secret": "secret"}
    assert calls[2][2]["data"] == {"file_type": "stream", "file_name": "a.html"}
    assert calls[2][2]["headers"] == {"Authorization": "Bearer t-1"}
    assert calls[3][2]["json"] == {"receive_id": "oc_1", "msg_type": "file", "content": '{"file_key": "file_1"}'}


def test_feishu_app_explains_what_went_wrong():
    two = [{"chat_id": "oc_1", "name": "甲"}, {"chat_id": "oc_2", "name": "乙"}]
    with pytest.raises(feishu_app.FeishuError, match="还没有加入任何群"):
        feishu_app.deliver("a", "b", "", "a.html", b"", fake_feishu([], []))
    with pytest.raises(feishu_app.FeishuError, match="多个群"):
        feishu_app.deliver("a", "b", "", "a.html", b"", fake_feishu([], two))
    with pytest.raises(feishu_app.FeishuError, match="不在之前选的那个群"):
        feishu_app.deliver("a", "b", "oc_9", "a.html", b"", fake_feishu([], two))
    assert feishu_app.deliver("a", "b", "oc_2", "a.html", b"", fake_feishu([], two))["name"] == "乙"
    with pytest.raises(feishu_app.FeishuError, match="上传文件没有成功：没有权限（飞书错误码 99991）"):
        feishu_app.deliver("a", "b", "oc_2", "a.html", b"", fake_feishu([], two, fail="files"))


def test_publish_sends_file_before_card_and_reports_file_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "app_ready", lambda root: True)
    order: list = []
    card = lambda title, subtitle, body: order.append(("card", body)) or True  # noqa: E731

    def good(name: str, content: bytes):
        order.append(("file", name, content[:15]))

    result = routes.publish(tmp_path, digest(), card, good)
    assert result["sent"] is True and result["file_error"] is None
    assert order[0] == ("file", "2026-11-19 每日汇总.html", b"<!doctype html>") and order[1][0] == "card"
    assert "已作为文件发在上一条消息里" in order[1][1]

    order.clear()
    result = routes.publish(tmp_path, digest(), card, lambda name, content: "上传文件没有成功")
    assert result["sent"] is True and result["file_error"] == "上传文件没有成功"
    assert "网页文件没有发出去：上传文件没有成功" in order[0][1] and "要在装了面板的这台电脑上打开" in order[0][1]

    monkeypatch.setattr(routes, "app_ready", lambda root: False)  # 没配自建应用：不去发文件
    order.clear()
    routes.publish(tmp_path, digest(), card, good)
    assert [o[0] for o in order] == ["card"]


def test_file_sender_remembers_the_chat(tmp_path, monkeypatch):
    from app import secrets_store

    monkeypatch.setattr(secrets_store, "load", lambda: {routes.SECRET_KEY: "secret"})
    routes._write_json(tmp_path / "config.json", {"app_id": "cli_x"})
    send = routes.file_sender(tmp_path, fake_feishu([], [{"chat_id": "oc_1", "name": "我的群"}]))
    assert send("a.html", b"x") is None
    assert routes.load_config(tmp_path)["chat_id"] == "oc_1" and routes.load_config(tmp_path)["chat_name"] == "我的群"
    assert "没有权限" in routes.file_sender(tmp_path, fake_feishu([], [], fail="internal"))("a.html", b"x")


def test_publish_mails_the_page_and_tells_the_card(tmp_path):
    cards: list = []
    mails: list = []
    card = lambda title, subtitle, body: cards.append(body) or True  # noqa: E731

    def mail(subject: str, text: str, page: str):
        mails.append((subject, text, page))
        return True

    result = routes.publish(tmp_path, digest(), card, None, mail)
    assert result["mailed"] is True and result["sent"] is True
    subject, text, page = mails[0]
    assert subject == "2026-11-19 每日汇总" and page.startswith("<!doctype html>")
    assert text.split("\n")[0] == "上证指数 -0.28%" and text.endswith("详情见附件。") and len(text.split("\n")) == 4
    assert cards[0].startswith("完整网页已发到你的邮箱")

    assert routes.publish(tmp_path, digest(), card, None, lambda *a: False)["mailed"] is False
    assert "邮件没有发出去" in cards[1]
    assert routes.publish(tmp_path, digest(), card, None, lambda *a: None)["mailed"] is None  # 没配邮箱
    assert "邮箱" not in cards[2]
    result = routes.publish(tmp_path, digest(), None, None, mail)  # 关掉飞书卡片：只发邮件
    assert len(mails) == 2 and len(cards) == 3 and result["sent"] is None and routes.delivered(result)
    assert not routes.delivered(routes.publish(tmp_path, digest(), None))  # 只生成网页


def test_channels_follow_the_feishu_card_switch(tmp_path):
    card, mail = (lambda *a: True), (lambda *a: True)
    send, send_file, send_mail = routes.channels(tmp_path, card, None, mail)
    assert send is card and send_file is not None and send_mail is mail
    routes._write_json(tmp_path / "config.json", {"feishu_card": False})
    assert routes.channels(tmp_path, card, None, mail) == (None, None, mail)


def test_publish_builds_the_page_from_screenshots(tmp_path):
    mails: list = []
    taken = [{"title": "看板", "image": b"\xff\xd8jpeg"}, {"title": "连板梯队", "error": "超时 <x>"}]
    result = routes.publish(tmp_path, digest(), None, None, lambda *a: mails.append(a) or True, lambda root: taken)
    assert result["shots"] == 1 and result["shots_failed"] == ["连板梯队"]
    page = (tmp_path / "pages" / "2026-11-19.html").read_text(encoding="utf-8")
    assert 'src="data:image/jpeg;base64,/9hqcGVn"' in page and "这一页没有截下来：超时 &lt;x&gt;" in page
    assert '<a href="#s1">连板梯队</a>' in page and "我的持仓" not in page
    assert mails[0][1].endswith("详情见附件（看板）。")
    assert render.brief_html("甲\n<乙>").count("<p") == 2 and "&lt;乙&gt;" in render.brief_html("甲\n<乙>")
    # 一张都没截下来：退回文字版
    routes.publish(tmp_path, digest(), None, None, None, lambda root: [{"title": "看板", "error": "x"}])
    assert "我的持仓" in (tmp_path / "pages" / "2026-11-19.html").read_text(encoding="utf-8")


def test_font_env_links_windows_fonts_only_when_present(tmp_path):
    from app.custom.daily_digest import shots

    assert shots.font_env(tmp_path / "work", tmp_path / "none") == {}
    source = tmp_path / "Fonts"
    source.mkdir()
    (source / "msyh.ttc").write_bytes(b"font")
    env = shots.font_env(tmp_path / "work", source)
    conf = (tmp_path / "work" / "fonts.conf").read_text(encoding="utf-8")
    assert env == {"FONTCONFIG_FILE": str(tmp_path / "work" / "fonts.conf")}
    assert (tmp_path / "work" / "fonts" / "msyh.ttc").read_bytes() == b"font" and "/etc/fonts/fonts.conf" in conf
    assert shots.font_env(tmp_path / "work", source) == env  # 再来一次不出错

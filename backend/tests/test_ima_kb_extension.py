"""腾讯 ima 知识库扩展：用假的接口测，不联网、不用真凭证。"""

from __future__ import annotations

import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.ima_kb import EXTENSION_ID, routes, service

PREFIX = routes.PREFIX
GOOD = {"client_id": "client-abcdefgh1234", "api_key": "key-secret-value-5678"}


@pytest.fixture
def env(tmp_path):
    calls: list[tuple[str, dict, dict]] = []
    state = {"retcode": 0, "status": 200, "fail_base": None}

    def post(url, headers, body, timeout):
        name = url.rsplit("/", 1)[1]
        calls.append((name, dict(headers), dict(body)))
        if state["status"] != 200:
            return state["status"], ""
        if headers.get("ima-openapi-apikey") != GOOD["api_key"]:
            return 200, json.dumps({"retcode": 110030, "errmsg": "无权限"})
        if state["retcode"]:
            return 200, json.dumps({"retcode": state["retcode"], "errmsg": "出错了"})
        if name == "get_addable_knowledge_base_list":
            data = {"addable_knowledge_base_list": [{"id": "kb1", "name": "研报"}, {"id": "kb2", "name": "笔记"}],
                    "is_end": True}
        elif name == "search_knowledge_base":
            data = {"info_list": [{"id": "kb9", "name": "订阅的" + body["query"]}], "is_end": True}
        elif name == "search_knowledge":
            if body["knowledge_base_id"] == state["fail_base"]:
                return 200, json.dumps({"retcode": 110011, "errmsg": "知识库不存在"})
            data = {"info_list": [
                {"media_id": "m1", "title": f"{body['query']}深度报告",
                 "highlight_content": f"<em>{body['query']}</em> 三季度\n营收增长 " + "很长" * 300},
                {"folder_id": "f1", "name": "文件夹"},  # 文件夹没有 media_id，不算结果
                {"media_id": "m2", "title": "只命中标题", "highlight_content": ""},
            ], "is_end": True}
        else:
            return 200, json.dumps({"retcode": 110012, "errmsg": "接口无效"})
        return 200, json.dumps({"retcode": 0, "errmsg": "成功", "data": data})

    service.set_transport(post, lambda seconds: None)
    routes._services.clear()
    routes._names.clear()
    app = FastAPI()
    data_dir = tmp_path / "tsp-data"
    app.state.repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=data_dir))
    app.include_router(routes.build_router())
    yield TestClient(app), calls, state, app, data_dir
    service.set_transport(None)
    routes._services.clear()


def ready(client):
    assert client.put(f"{PREFIX}/credentials", json=GOOD).status_code == 200
    client.put(f"{PREFIX}/selected", json={"selected": [{"id": "kb1", "name": "研报"}, {"id": "kb2", "name": "笔记"}]})


def test_identity_and_empty_config(env):
    client, _, _, _, _ = env
    assert EXTENSION_ID == "community.ima-kb"
    assert client.get(f"{PREFIX}/config").json() == {
        "configured": False, "client_id_masked": "", "selected": [], "use_in_ai": True, "max_selected": 10}
    assert client.post(f"{PREFIX}/search", json={"query": "茅台"}).status_code == 400  # 还没填凭证


def test_credentials_are_checked_before_saving_and_never_returned(env):
    client, calls, _, _, data_dir = env
    bad = client.put(f"{PREFIX}/credentials", json={"client_id": "x", "api_key": "wrong"})
    assert bad.status_code == 400 and "无权限" in bad.json()["detail"]
    assert client.get(f"{PREFIX}/config").json()["configured"] is False  # 不能用的凭证不保存

    saved = client.put(f"{PREFIX}/credentials", json=GOOD)
    assert saved.status_code == 200 and saved.json()["configured"] is True
    assert saved.json()["client_id_masked"] == "clie…1234"
    assert GOOD["api_key"] not in saved.text and GOOD["client_id"] not in client.get(f"{PREFIX}/config").text
    name, headers, _ = calls[-1]
    assert name == "get_addable_knowledge_base_list"
    assert headers["ima-openapi-clientid"] == GOOD["client_id"] and headers["ima-openapi-apikey"] == GOOD["api_key"]
    stored = data_dir / "user_data" / "extensions" / "ima_kb" / "config.json"
    assert stored.stat().st_mode & 0o077 == 0  # 只有本机用户可读

    assert client.delete(f"{PREFIX}/credentials").json()["configured"] is False


def test_listing_and_choosing_knowledge_bases(env):
    client, _, _, _, _ = env
    client.put(f"{PREFIX}/credentials", json=GOOD)
    assert client.get(f"{PREFIX}/knowledge-bases").json()["items"] == [
        {"id": "kb1", "name": "研报"}, {"id": "kb2", "name": "笔记"}]
    assert client.get(f"{PREFIX}/knowledge-bases", params={"query": "白酒"}).json()["items"] == [
        {"id": "kb9", "name": "订阅的白酒"}]
    chosen = client.put(f"{PREFIX}/selected", json={"selected": [
        {"id": "kb1", "name": "研报"}, {"id": "kb1", "name": "重复的"}, {"id": "kb9", "name": ""}]}).json()
    assert chosen["selected"] == [{"id": "kb1", "name": "研报"}, {"id": "kb9", "name": "kb9"}]
    too_many = client.put(f"{PREFIX}/selected", json={"selected": [{"id": f"k{i}", "name": "x"} for i in range(11)]})
    assert too_many.status_code == 400 and "最多选 10 个" in too_many.json()["detail"]


def test_search_goes_through_each_selected_base_and_cleans_snippets(env):
    client, calls, state, _, _ = env
    ready(client)
    calls.clear()
    result = client.post(f"{PREFIX}/search", json={"query": " 贵州茅台 "}).json()
    assert [c[0] for c in calls] == ["search_knowledge", "search_knowledge"]
    assert [c[2]["knowledge_base_id"] for c in calls] == ["kb1", "kb2"] and calls[0][2]["query"] == "贵州茅台"
    assert len(result["items"]) == 4 and result["errors"] == []
    first = result["items"][0]
    assert first["base_name"] == "研报" and first["title"] == "贵州茅台深度报告"
    assert first["snippet"].startswith("贵州茅台 三季度 营收增长") and "<em>" not in first["snippet"]
    assert len(first["snippet"]) == service.SNIPPET_CHARS + 1 and first["snippet"].endswith("…")

    # 一个知识库出错不影响别的
    state["fail_base"] = "kb2"
    partial = client.post(f"{PREFIX}/search", json={"query": "茅台"}).json()
    assert len(partial["items"]) == 2 and partial["errors"] == ["笔记：ima：知识库不存在"]
    only = client.post(f"{PREFIX}/search", json={"query": "茅台", "base_ids": ["kb1"]}).json()
    assert {i["base_id"] for i in only["items"]} == {"kb1"}
    assert client.post(f"{PREFIX}/search", json={"query": "  "}).status_code == 400


def test_errors_are_explained(env):
    client, _, state, _, _ = env
    ready(client)
    state["retcode"] = 110021
    limited = client.get(f"{PREFIX}/knowledge-bases")
    assert limited.status_code == 502 and "太频繁" in limited.json()["detail"]
    state["retcode"], state["status"] = 0, 401
    refused = client.get(f"{PREFIX}/knowledge-bases")
    assert refused.status_code == 400 and "拒绝了这组凭证" in refused.json()["detail"]


def test_stock_lookup_uses_the_stock_name(env):
    import polars as pl

    client, calls, _, _, data_dir = env
    ready(client)
    (data_dir / "instruments").mkdir(parents=True)
    pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"]}).write_parquet(
        data_dir / "instruments" / "instruments.parquet")
    calls.clear()
    assert client.get(f"{PREFIX}/stock/600519.sh").json()["query"] == "贵州茅台"
    assert client.get(f"{PREFIX}/stock/000001.SZ").json()["query"] == "000001"  # 清单里没有：按代码搜


def test_ai_context_only_for_prompts_about_a_single_stock(env):
    import polars as pl

    client, calls, _, app, data_dir = env
    (data_dir / "instruments").mkdir(parents=True)
    pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"]}).write_parquet(
        data_dir / "instruments" / "instruments.parquet")
    one = "标的标准代码: 600519.SH\n最近 60 日 K 线……再提一次 600519.SH"
    assert routes.ai_context(app, one) == ""  # 还没配置：什么都不加
    ready(client)
    calls.clear()

    text = routes.ai_context(app, one)
    assert "我的知识库里和“贵州茅台”相关的内容" in text and "《贵州茅台深度报告》（研报）" in text
    assert "只命中标题" not in text  # 没有片段的不给 AI
    assert "以行情数据为准" in text
    assert len(calls) == 2
    routes.ai_context(app, one)
    assert len(calls) == 2  # 同一只股票十分钟内不重复搜

    assert routes.ai_context(app, "今天大盘怎么样") == ""  # 没有股票代码
    assert routes.ai_context(app, "600519.SH 和 000001.SZ 哪个强") == ""  # 提到多只：不搜
    assert routes.ai_context(app, "订单号 1600519.SH9") == ""  # 不是股票代码
    client.put(f"{PREFIX}/options", json={"use_in_ai": False})
    assert routes.ai_context(app, one) == ""  # 开关关了


def test_helpers():
    assert service.mask("abcd") == "abcd" and service.mask("abcdefghijkl") == "abcd…ijkl"
    assert service.clean_snippet("<em>a</em>\n b  <span class='x'>c</span>") == "a b c"

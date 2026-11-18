"""“左上角的话”扩展的测试。评分是造的，语料库写在临时目录里，不碰真实数据。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.market_motto import EXTENSION_API_VERSION, EXTENSION_ID, corpus, routes
from app.extensions import BACKEND_EXTENSION_API_VERSION

TEXT = """# 备注
冷（0-40）
甲
乙
丙

热(40~100)
丁
"""


def test_extension_identity():
    assert EXTENSION_ID == "community.market-motto"
    assert EXTENSION_API_VERSION == BACKEND_EXTENSION_API_VERSION


def test_default_corpus_is_clean_and_covers_every_score():
    parsed = corpus.parse(corpus.DEFAULT_TEXT)
    assert parsed.problems == [] and parsed.notes == []
    assert all(parsed.band_of(score) is not None for score in range(0, 101))
    assert all(len(line) <= corpus.MAX_CHARS for band in parsed.bands for line in band.lines)


def test_parse_reads_bands_with_either_bracket_style():
    parsed = corpus.parse(TEXT)
    assert [(b.name, b.low, b.high, b.lines) for b in parsed.bands] == [
        ("冷", 0, 40, ["甲", "乙", "丙"]), ("热", 40, 100, ["丁"])]
    assert parsed.problems == [] and parsed.notes == []


def test_band_edges():
    parsed = corpus.parse(TEXT)
    assert parsed.band_of(0).name == "冷"
    assert parsed.band_of(39.9).name == "冷"
    assert parsed.band_of(40).name == "热"  # 下限含，上限不含
    assert parsed.band_of(100).name == "热"  # 上限是 100 时含 100


def test_pick_is_stable_within_a_day_and_changes_across_days():
    parsed = corpus.parse(TEXT)
    first, band = parsed.pick(10, "2026-11-18")
    assert band.name == "冷" and first in {"甲", "乙", "丙"}
    assert all(parsed.pick(score, "2026-11-18")[0] == first for score in (0, 20, 39))
    days = {parsed.pick(10, f"2026-11-{d:02d}")[0] for d in range(1, 29)}
    assert days == {"甲", "乙", "丙"}  # 换着来，每句都轮得到


def test_pick_falls_back_when_score_is_outside_every_band():
    parsed = corpus.parse("冷（0-30）\n甲\n热（60-100）\n乙\n空（30-40）\n")
    assert parsed.pick(50, "2026-11-18") == (corpus.FALLBACK, None)
    assert parsed.pick(35, "2026-11-18") == (corpus.FALLBACK, None)  # 这一档没有话
    assert any("40 到 60" in note for note in parsed.notes)
    assert any("空" in note for note in parsed.notes)


@pytest.mark.parametrize("text, word", [
    ("甲\n冷（0-30）\n乙", "前面还没有档位"),
    ("冷（30-10）\n甲", "小-大"),
    ("冷（0-120）\n甲", "不超过 100"),
    ("冷（0-50）\n甲\n热（40-100）\n乙", "重叠"),
    ("# 只有备注", "至少要有一个档位"),
    ("冷（0-50）", "至少要有一个档位"),
])
def test_parse_reports_problems(text, word):
    assert any(word in problem for problem in corpus.parse(text).problems)


def test_long_lines_are_noted_not_rejected():
    parsed = corpus.parse("冷（0-100）\n" + "长" * 20)
    assert parsed.problems == [] and "20 个字" in parsed.notes[0]


def test_current_without_score_and_with_broken_corpus():
    assert routes.current(TEXT, None) == {"text": corpus.FALLBACK, "score": None, "label": None, "band": None, "as_of": None}
    broken = routes.current("没有档位的一句", {"score": 20.0, "label": "冰点", "as_of": "2026-11-18"})
    assert broken["band"] == "冰点"
    assert broken["text"] in corpus.parse(corpus.DEFAULT_TEXT).bands[0].lines  # 改坏了就用自带的那份


def test_cached_recomputes_only_after_ttl():
    now, calls = [0.0], []
    cached = routes.Cached(30, clock=lambda: now[0])
    load = lambda: calls.append(1) or len(calls)  # noqa: E731
    assert cached.get(load) == 1
    now[0] = 29
    assert cached.get(load) == 1
    now[0] = 30
    assert cached.get(load) == 2


@pytest.fixture
def client(tmp_path):
    state = {"score": {"score": 20.0, "label": "冰点", "as_of": "2026-11-18"}}

    def get_score(_app_state):
        if isinstance(state["score"], Exception):
            raise state["score"]
        return state["score"]

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(routes.build_router(get_score))
    return TestClient(app), state, routes.corpus_path(tmp_path)


def test_endpoints_roundtrip(client):
    http, _, path = client
    body = http.get("/api/custom/market-motto/corpus").json()
    assert body["is_default"] is True and body["text"] == corpus.DEFAULT_TEXT and body["path"] == str(path)
    assert not path.exists()  # 只是看，不会创建文件

    now = http.get("/api/custom/market-motto/current").json()
    assert now["band"] == "冰点" and now["score"] == 20.0 and now["as_of"] == "2026-11-18"

    saved = http.put("/api/custom/market-motto/corpus", json={"text": TEXT}).json()
    assert saved["is_default"] is False and [b["count"] for b in saved["bands"]] == [3, 1]
    assert path.read_text(encoding="utf-8") == TEXT
    assert http.get("/api/custom/market-motto/current").json()["band"] == "冷"

    reset = http.delete("/api/custom/market-motto/corpus").json()
    assert reset["is_default"] is True and not path.exists()


def test_put_rejects_a_broken_corpus_and_keeps_the_old_one(client):
    http, _, path = client
    http.put("/api/custom/market-motto/corpus", json={"text": TEXT})
    response = http.put("/api/custom/market-motto/corpus", json={"text": "冷（0-50）\n甲\n热（40-100）\n乙"})
    assert response.status_code == 400 and "重叠" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == TEXT


def test_current_survives_a_failing_score(client):
    http, state, _ = client
    state["score"] = RuntimeError("算不出来")
    body = http.get("/api/custom/market-motto/current").json()
    assert body["text"] == corpus.FALLBACK and body["score"] is None

"""市场环境盘中刷新：节流、互斥、管道在跑时不动。用假的计算函数，不碰真实数据。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.regime_live import routes


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _worker(results, busy=lambda: False):
    calls = []
    clock = _Clock()

    def run(repo, data_dir):
        calls.append(data_dir)
        return results[min(len(calls), len(results)) - 1]

    return routes.Refresher(run=run, busy=busy, clock=clock, min_gap=30), calls, clock


def test_refresh_reports_what_was_recomputed(tmp_path):
    worker, calls, _ = _worker([{"days": 1}])
    assert worker.refresh(None, tmp_path) == {"updated": True, "days": 1}
    assert calls == [tmp_path]


def test_nothing_changed_is_not_an_update(tmp_path):
    worker, _, _ = _worker([{"days": 0}])
    assert worker.refresh(None, tmp_path)["updated"] is False


def test_refresh_is_throttled(tmp_path):
    worker, calls, clock = _worker([{"days": 1}])
    worker.refresh(None, tmp_path)
    clock.now += 10
    assert worker.refresh(None, tmp_path) == {"updated": False, "reason": "recent"}
    assert len(calls) == 1
    clock.now += 25
    assert worker.refresh(None, tmp_path)["updated"] is True
    assert len(calls) == 2


def test_skips_while_pipeline_is_running(tmp_path):
    worker, calls, _ = _worker([{"days": 1}], busy=lambda: True)
    assert worker.refresh(None, tmp_path) == {"updated": False, "reason": "busy"}
    assert calls == []


def test_endpoint(tmp_path):
    worker, calls, _ = _worker([{"days": 1}])
    app = FastAPI()
    app.include_router(routes.build_router(worker))
    client = TestClient(app)
    assert client.post("/api/custom/regime-live/refresh").status_code == 503  # 数据目录还没就绪
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert client.post("/api/custom/regime-live/refresh").json() == {"updated": True, "days": 1}
    assert calls == [Path(tmp_path)]

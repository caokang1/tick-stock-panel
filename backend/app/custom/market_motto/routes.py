"""左上角的话的接口：当前该显示哪一句，以及语料库的读和存。"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from . import corpus

logger = logging.getLogger(__name__)

PREFIX = "/api/custom/market-motto"
SCORE_TTL = 30.0  # 评分最多每 30 秒重算一次；它只读本机数据，不联网

Score = dict[str, Any]  # {"score": 36, "label": "偏冷", "as_of": "2026-11-18"}


def corpus_path(data_dir: Path) -> Path:
    return data_dir / "user_data" / "extensions" / "market_motto" / "corpus.txt"


def read_text(path: Path) -> tuple[str, bool]:
    """语料库的内容，以及是不是还在用自带的那份（自己的文件不存在或读不了）。"""
    try:
        return path.read_text(encoding="utf-8"), False
    except OSError:
        return corpus.DEFAULT_TEXT, True


def score_of(app_state: Any) -> Score | None:
    """面板的情绪评分。实时行情开着时是盘中的，关着时是最近一个交易日收盘的。没有数据时返回 None。"""
    from app.services.market_overview_builder import build_market_overview

    overview = build_market_overview(
        repo=app_state.repo,
        quote_service=getattr(app_state, "quote_service", None),
        depth_service=getattr(app_state, "depth_service", None),
        as_of=None,
    )
    emotion, as_of = overview.get("emotion") or {}, overview.get("as_of")
    if not as_of or emotion.get("score") is None or emotion.get("label") == "暂无":
        return None
    return {"score": float(emotion["score"]), "label": str(emotion.get("label") or ""), "as_of": str(as_of)[:10]}


class Cached:
    """短时间内重复来问只算一次。"""

    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: Any = None

    def get(self, load: Callable[[], Any]) -> Any:
        with self._lock:
            if self._at is None or self._clock() - self._at >= self._ttl:
                self._value, self._at = load(), self._clock()
            return self._value


def current(text: str, score: Score | None) -> dict[str, Any]:
    """语料库 + 评分 → 左上角显示什么。"""
    if score is None:
        return {"text": corpus.FALLBACK, "score": None, "label": None, "band": None, "as_of": None}
    parsed = corpus.parse(text)
    if parsed.problems:  # 手改文件改坏了：退回自带的那份，不让左上角空着
        parsed = corpus.parse(corpus.DEFAULT_TEXT)
    line, band = parsed.pick(score["score"], score["as_of"])
    return {"text": line, "score": score["score"], "label": score["label"],
            "band": band.name if band else None, "as_of": score["as_of"]}


class CorpusIn(BaseModel):
    text: str


def build_router(get_score: Callable[[Any], Score | None] = score_of) -> APIRouter:
    router = APIRouter(prefix=PREFIX, tags=["custom-market-motto"])
    cached = Cached(SCORE_TTL)

    def path_of(request: Request) -> Path:
        raw = getattr(getattr(getattr(request.app.state, "repo", None), "store", None), "data_dir", None)
        if not isinstance(raw, (str, Path)):
            raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
        return corpus_path(Path(raw))

    def safe_score(request: Request) -> Score | None:
        try:
            return cached.get(lambda: get_score(request.app.state))
        except Exception:  # noqa: BLE001 —— 评分算不出来时显示默认那句，不能让侧栏报错
            logger.exception("情绪评分没有算出来")
            return None

    @router.get("/current")
    def get_current(request: Request) -> dict[str, Any]:
        """现在左上角该显示的那句话，以及它是按哪个评分、哪一档挑的。"""
        text, _ = read_text(path_of(request))
        return current(text, safe_score(request))

    @router.get("/corpus")
    def get_corpus(request: Request) -> dict[str, Any]:
        path = path_of(request)
        text, is_default = read_text(path)
        parsed = corpus.parse(text)
        return {"text": text, "is_default": is_default, "path": str(path),
                "problems": parsed.problems, "notes": parsed.notes, "max_chars": corpus.MAX_CHARS,
                "bands": [{"name": b.name, "low": b.low, "high": b.high, "count": len(b.lines)} for b in parsed.bands]}

    @router.put("/corpus")
    def put_corpus(body: CorpusIn, request: Request) -> dict[str, Any]:
        """保存语料库。格式有问题时不保存，把问题一条条说出来。"""
        if len(body.text) > corpus.MAX_TEXT:
            raise HTTPException(status_code=400, detail="内容太长了。")
        parsed = corpus.parse(body.text)
        if parsed.problems:
            raise HTTPException(status_code=400, detail=" ".join(parsed.problems))
        path = path_of(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(body.text, encoding="utf-8")
        os.replace(temp, path)
        return get_corpus(request)

    @router.delete("/corpus")
    def reset_corpus(request: Request) -> dict[str, Any]:
        """删掉自己的语料库，回到自带的那份。"""
        path_of(request).unlink(missing_ok=True)
        return get_corpus(request)

    return router

"""把本机的 claude 命令行包装成一个“OpenAI 兼容接口”，供 TSP 的“设置 → AI”使用。

用法：在 TSP 的“设置 → AI”里选“OpenAI 兼容”，地址填 ``http://127.0.0.1:<后端端口>/claude-bridge/v1``，
API Key 填本扩展生成的转接口令（见 ``GET /api/custom/claude-bridge/info``），模型填 ``claude-cli``
（用命令行的默认模型）或 ``sonnet`` / ``opus`` 这样的别名。

安全上的处理：

- 只接受本机（回环地址）发来的请求，并且必须带对转接口令——TSP 的后端默认监听所有网卡，
  没有这两道闸，同一个局域网里的人就能借这台电脑的 Claude 账号用；
- 调用命令行时关掉它的全部工具（``--tools ""``）、不加载任何 MCP 服务、不保存会话，在一个空目录里运行，
  它只能根据发过去的文字生成文字，碰不到本机的文件；
- 同时最多跑 ``MAX_PARALLEL`` 个，单次最长 ``TIMEOUT_SECONDS`` 秒。

限制：不支持工具调用（请求里的 ``tools`` 会被忽略，只返回文字）；``temperature`` 等采样参数不起作用。
路径没有放在 ``/api/`` 下面：OpenAI 的 SDK 会带 ``Authorization: Bearer``，而 TSP 把 ``/api/`` 下的 Bearer
当作它自己的开放接口令牌来验，会直接拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

PREFIX = "/claude-bridge/v1"
INFO_PREFIX = "/api/custom/claude-bridge"
DEFAULT_MODEL = "claude-cli"
DEFAULT_SYSTEM = "你是一个严谨、简洁的中文助手。只根据用户给出的材料回答，不要编造数据。"
MAX_PARALLEL = 2
TIMEOUT_SECONDS = 600.0
MAX_PROMPT_CHARS = 600_000
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# 从面板进程继承下来、会让命令行误以为自己嵌套在另一个会话里的环境变量
_DROP_ENV_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_")

_slots = asyncio.Semaphore(MAX_PARALLEL)


# ---------------- 命令行在哪、口令是什么 ----------------
def find_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None


def _root(request: Request) -> Path:
    repo = getattr(request.app.state, "repo", None)
    raw = getattr(getattr(repo, "store", None), "data_dir", None)
    if not isinstance(raw, (str, Path)):
        raise HTTPException(status_code=503, detail="数据目录还没有就绪，请稍后再试。")
    root = Path(raw) / "user_data" / "extensions" / "claude_bridge"
    root.mkdir(parents=True, exist_ok=True)
    return root


def bridge_key(root: Path) -> str:
    """转接口令：第一次用到时生成，存在本机，只有本机用户读得到。"""
    path = root / "key.txt"
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = "cb-" + secrets.token_urlsafe(24)
    path.write_text(key, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return key


def _guard(request: Request) -> Path:
    """只放行本机发来的、带对口令的请求。返回扩展自己的数据目录。"""
    host = request.client.host if request.client else ""
    if host not in _LOOPBACK:
        raise HTTPException(status_code=403, detail="Claude 转接口只接受本机的请求。")
    root = _root(request)
    given = request.headers.get("authorization", "")
    token = given[7:].strip() if given.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token, bridge_key(root)):
        raise HTTPException(status_code=401, detail="转接口令不对。请在“设置 → AI”的 API Key 里填转接口令。")
    return root


# ---------------- OpenAI 的消息 → 给命令行的文字 ----------------
def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 多段内容：只取文字段
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def build_prompt(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """返回（系统提示, 发给命令行的正文）。只有一条用户消息时原样发；多轮对话拼成一份带角色的记录。"""
    system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") in ("system", "developer")).strip()
    turns = [(str(m.get("role")), _text(m.get("content")).strip()) for m in messages
             if m.get("role") in ("user", "assistant", "tool")]
    turns = [(role, text) for role, text in turns if text]
    if not turns:
        raise HTTPException(status_code=400, detail="请求里没有可以回答的内容。")
    if len(turns) == 1 and turns[0][0] == "user":
        prompt = turns[0][1]
    else:
        names = {"user": "用户", "assistant": "助手", "tool": "工具返回"}
        record = "\n\n".join(f"【{names[role]}】\n{text}" for role, text in turns)
        prompt = f"下面是到目前为止的对话记录。请接着以助手的身份回答最后一条用户消息，只输出回答本身。\n\n{record}"
    if len(prompt) + len(system) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="发给 AI 的内容太长了，请缩短后再试。")
    return (system or DEFAULT_SYSTEM) + ANSWER_RULE, prompt


def build_command(cli: str, system: str, model: str | None, *, stream: bool) -> list[str]:
    command = [
        cli, "-p", "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
        "--system-prompt", system,
    ]
    if stream:
        command += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
    else:
        command += ["--output-format", "json"]
    if model and model != DEFAULT_MODEL:
        command += ["--model", model]
    return command


def _environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(_DROP_ENV_PREFIXES)}


async def _spawn(command: list[str], workdir: Path) -> asyncio.subprocess.Process:
    workdir.mkdir(parents=True, exist_ok=True)
    return await asyncio.create_subprocess_exec(
        *command, cwd=str(workdir), env=_environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # 流式输出的一行可能很长（整段回答在一行里），默认的 64 KB 不够
    )


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)


def _failure(stderr: bytes, stdout: bytes = b"") -> str:
    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return "本机的 claude 命令行没有成功返回" + (f"：{detail[-400:]}" if detail else "。")


# ---------------- 去掉回答开头夹带的思考过程 ----------------
_THINK_TAGS = ("reasoning", "thinking", "thinking_mode")
_THINK_BLOCK = re.compile(r"^\s*<(reasoning|thinking_mode|thinking)>.*?</\1>\s*", re.DOTALL)


def strip_thinking(text: str) -> str:
    """模型有时会在回答最前面带一段 <reasoning>…</reasoning>，那是它的草稿，不是给人看的。"""
    while True:
        cleaned = _THINK_BLOCK.sub("", text, count=1)
        if cleaned == text:
            return text
        text = cleaned


ANSWER_OPEN, ANSWER_CLOSE = "<answer>", "</answer>"
ANSWER_RULE = (
    "\n\n【输出格式】把给用户看的完整回答放在 <answer> 和 </answer> 之间，标签之外不要写任何内容"
    "（不要写思考过程、草稿或计划）。回答本身的内容和格式按上面的要求来。"
)


def extract_answer(text: str) -> str:
    """取 <answer>…</answer> 里面的内容；模型没有按要求加标签时，退回到去掉开头草稿后的全文。"""
    start = text.find(ANSWER_OPEN)
    if start < 0:
        return strip_thinking(text).strip()
    body = text[start + len(ANSWER_OPEN):]
    end = body.rfind(ANSWER_CLOSE)
    return (body if end < 0 else body[:end]).strip()


class AnswerStream:
    """流式输出用：只放行 <answer> 里面的内容。

    模型偶尔会在正式回答前面写一段草稿（不在思考块里，而是当成正文输出）。让它把正式回答放进标签里，
    标签之前的一律不外送；如果一直没有出现标签，结束时把攒下的全文（去掉开头的草稿标记）一次给出，不丢内容。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._state = "before"  # before → inside → after

    def feed(self, piece: str) -> str:
        if self._state == "after":
            return ""
        self._buffer += piece
        if self._state == "before":
            start = self._buffer.find(ANSWER_OPEN)
            if start < 0:
                return ""
            self._buffer = self._buffer[start + len(ANSWER_OPEN):].lstrip("\n")
            self._state = "inside"
        end = self._buffer.find(ANSWER_CLOSE)
        if end >= 0:
            out, self._buffer, self._state = self._buffer[:end].rstrip(), "", "after"
            return out
        # 末尾可能是半个结束标签，先留着不送
        keep = next((n for n in range(min(len(ANSWER_CLOSE) - 1, len(self._buffer)), 0, -1)
                     if ANSWER_CLOSE.startswith(self._buffer[-n:])), 0)
        out = self._buffer[:len(self._buffer) - keep]
        self._buffer = self._buffer[len(self._buffer) - keep:]
        return out

    def flush(self) -> str:
        out = ""
        if self._state == "before":
            out = strip_thinking(self._buffer).strip()
        elif self._state == "inside":
            out = self._buffer
        self._buffer, self._state = "", "after"
        return out


# ---------------- 一次调用 ----------------
async def run_once(cli: str, workdir: Path, system: str, prompt: str, model: str | None) -> tuple[str, dict[str, int]]:
    """跑一次命令行，返回（回答的文字, 用量）。失败时抛 HTTPException。"""
    async with _slots:
        process = await _spawn(build_command(cli, system, model, stream=False), workdir)
        try:
            out, err = await asyncio.wait_for(process.communicate(prompt.encode("utf-8")), timeout=TIMEOUT_SECONDS)
        except TimeoutError as e:
            await _stop(process)
            raise HTTPException(status_code=504, detail="本机的 claude 命令行超时了。") from e
        except BaseException:
            await _stop(process)  # 调用方断开或被取消：不留一个还在跑的进程
            raise
    try:
        body = json.loads(out.decode("utf-8", errors="replace"))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=_failure(err, out)) from e
    if process.returncode != 0 or not isinstance(body, dict) or body.get("is_error"):
        message = str(body.get("result") or "") if isinstance(body, dict) else ""
        raise HTTPException(status_code=502, detail=_failure(message.encode() or err, out if not message else b""))
    return extract_answer(str(body.get("result") or "")), _usage(body.get("usage"))


def _usage(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    prompt = sum(int(raw.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens"))
    completion = int(raw.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def delta_text(event: dict[str, Any]) -> str:
    """流式输出里的一行如果是一小段新的文字，就把这段文字取出来；别的行返回空。"""
    if event.get("type") != "stream_event":
        return ""
    inner = event.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "content_block_delta":
        return ""
    delta = inner.get("delta")
    if isinstance(delta, dict) and delta.get("type") == "text_delta":
        return str(delta.get("text") or "")
    return ""


async def run_stream(cli: str, workdir: Path, system: str, prompt: str, model: str | None,
                     shown_model: str) -> AsyncIterator[str]:
    """跑一次命令行，边生成边按 OpenAI 的流式格式往外送。"""
    ident, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        payload = {"id": ident, "object": "chat.completion.chunk", "created": created, "model": shown_model,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async with _slots:
        process = await _spawn(build_command(cli, system, model, stream=True), workdir)
        sent = False
        final = ""
        failed: str | None = None
        leading = AnswerStream()
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            yield chunk({"role": "assistant", "content": ""})
            deadline = time.monotonic() + TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failed = "本机的 claude 命令行超时了。"
                    break
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                except TimeoutError:
                    failed = "本机的 claude 命令行超时了。"
                    break
                except ValueError:  # 一行太长，超过了读缓冲：跳过，等最后的结果行
                    continue
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                piece = delta_text(event)
                if piece:
                    piece = leading.feed(piece)
                    if piece:
                        sent = True
                        yield chunk({"content": piece})
                elif event.get("type") == "result":
                    final = str(event.get("result") or "")
                    if event.get("is_error"):
                        failed = _failure(final.encode())
            if failed is None:
                await asyncio.wait_for(process.wait(), timeout=10)
                if process.returncode != 0 and not sent and not final:
                    assert process.stderr is not None
                    failed = _failure(await process.stderr.read())
            if failed is not None:
                yield chunk({"content": f"\n\n[{failed}]"})
            else:
                rest = leading.flush()
                if rest:
                    sent = True
                    yield chunk({"content": rest})
                if not sent and final:
                    yield chunk({"content": extract_answer(final)})  # 没有收到逐段的文字时，把完整的回答一次送出
            yield chunk({}, "stop")
            yield "data: [DONE]\n\n"
        finally:
            await _stop(process)


# ---------------- 路由 ----------------
def build_router() -> APIRouter:
    router = APIRouter(tags=["custom-claude-bridge"])

    @router.get(f"{INFO_PREFIX}/info")
    def info(request: Request) -> dict[str, Any]:
        """给本机用户看的配置说明（走面板自己的登录校验）：命令行在不在、地址和口令是什么。"""
        cli = find_cli()
        port = request.url.port or 3018
        return {
            "available": cli is not None, "command": cli,
            "base_url": f"http://127.0.0.1:{port}{PREFIX}", "api_key": bridge_key(_root(request)),
            "model": DEFAULT_MODEL,
            "notes": ["只接受本机的请求，并且要带转接口令。", "不支持工具调用，AI 助手只能纯聊天。",
                      "每次调用都会用到本机 claude 命令行登录的那个账号的额度。"],
        }

    @router.get(f"{PREFIX}/models")
    def models(request: Request) -> dict[str, Any]:
        _guard(request)
        names = [DEFAULT_MODEL, "sonnet", "opus", "haiku"]
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "claude-cli"} for n in names]}

    @router.post(f"{PREFIX}/chat/completions")
    async def chat_completions(request: Request) -> Any:
        root = _guard(request)
        cli = find_cli()
        if cli is None:
            raise HTTPException(status_code=503, detail="这台电脑上没有找到 claude 命令行。")
        try:
            body = await request.json()
        except ValueError as e:
            raise HTTPException(status_code=400, detail="请求不是合法的 JSON。") from e
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list):
            raise HTTPException(status_code=400, detail="请求里缺少 messages。")
        system, prompt = build_prompt([m for m in messages if isinstance(m, dict)])
        shown = str(body.get("model") or DEFAULT_MODEL)
        model = shown if shown != DEFAULT_MODEL else None
        workdir = root / "workdir"
        if body.get("stream"):
            return StreamingResponse(run_stream(cli, workdir, system, prompt, model, shown),
                                     media_type="text/event-stream")
        text, usage = await run_once(cli, workdir, system, prompt, model)
        return JSONResponse({
            "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
            "model": shown,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        })

    return router

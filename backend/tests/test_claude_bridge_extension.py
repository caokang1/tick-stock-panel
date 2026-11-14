"""Claude 转接口：用一个假的 claude 命令行（shell 脚本）测，不联网、不用真账号。"""

from __future__ import annotations

import json
import stat
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom.claude_bridge import EXTENSION_ID, bridge

V1 = bridge.PREFIX
INFO = f"{bridge.INFO_PREFIX}/info"

FAKE_CLI = r"""#!/bin/sh
# 假的 claude：把收到的参数和标准输入记下来，按要求的格式回一段话
dir="$(dirname "$0")"
printf '%s\n' "$@" > "$dir/args.txt"
cat > "$dir/stdin.txt"
if [ -f "$dir/fail" ]; then echo "not logged in" >&2; exit 1; fi
case "$*" in
  *stream-json*)
    echo '{"type":"system","subtype":"init"}'
    echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"想一下"}}}'
    echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}}'
    echo '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}}'
    echo '{"type":"result","is_error":false,"result":"你好"}'
    ;;
  *)
    echo '{"type":"result","is_error":false,"result":"你好","usage":{"input_tokens":3,"cache_read_input_tokens":7,"output_tokens":2}}'
    ;;
esac
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text(FAKE_CLI, encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(bridge, "find_cli", lambda: str(cli))
    app = FastAPI()
    app.state.repo = types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path / "tsp-data"))
    app.include_router(bridge.build_router())
    local = TestClient(app, client=("127.0.0.1", 50000))
    key = local.get(INFO).json()["api_key"]
    return app, local, {"Authorization": f"Bearer {key}"}, cli.parent


def test_extension_identity_and_info(env):
    _, local, _, _ = env
    assert EXTENSION_ID == "community.claude-bridge"
    info = local.get(INFO).json()
    assert info["available"] is True and info["base_url"].endswith("/claude-bridge/v1")
    assert info["api_key"].startswith("cb-") and local.get(INFO).json()["api_key"] == info["api_key"]  # 口令固定


def test_only_this_computer_with_the_right_key_may_call(env):
    app, local, auth, _ = env
    body = {"model": "claude-cli", "messages": [{"role": "user", "content": "hi"}]}
    outside = TestClient(app, client=("192.168.1.20", 50000))
    assert outside.post(f"{V1}/chat/completions", json=body, headers=auth).status_code == 403
    assert local.post(f"{V1}/chat/completions", json=body).status_code == 401
    assert local.post(f"{V1}/chat/completions", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert local.get(f"{V1}/models").status_code == 401
    assert local.get(f"{V1}/models", headers=auth).json()["data"][0]["id"] == "claude-cli"


def test_plain_answer_in_openai_shape_and_cli_runs_without_tools(env):
    _, local, auth, folder = env
    body = {"model": "claude-cli", "temperature": 0.2, "tools": [{"type": "function"}],
            "messages": [{"role": "system", "content": "你是分析师"}, {"role": "user", "content": "总结一下"}]}
    answer = local.post(f"{V1}/chat/completions", json=body, headers=auth)
    assert answer.status_code == 200, answer.text
    data = answer.json()
    assert data["object"] == "chat.completion" and data["choices"][0]["message"] == {"role": "assistant", "content": "你好"}
    assert data["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    args = (folder / "args.txt").read_text(encoding="utf-8").split("\n")
    assert args[0] == "-p" and args[args.index("--tools") + 1] == ""  # 全部工具关掉
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert args[args.index("--system-prompt") + 1] == "你是分析师"
    assert "--model" not in args  # claude-cli 表示用命令行的默认模型
    assert (folder / "stdin.txt").read_text(encoding="utf-8") == "总结一下"  # 正文走标准输入，不占命令行长度


def test_model_alias_and_multi_turn_conversation(env):
    _, local, auth, folder = env
    body = {"model": "sonnet", "messages": [
        {"role": "user", "content": "第一问"}, {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": [{"type": "text", "text": "第二问"}, {"type": "image_url", "image_url": {}}]}]}
    assert local.post(f"{V1}/chat/completions", json=body, headers=auth).status_code == 200
    args = (folder / "args.txt").read_text(encoding="utf-8").split("\n")
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--system-prompt") + 1] == bridge.DEFAULT_SYSTEM
    sent = (folder / "stdin.txt").read_text(encoding="utf-8")
    assert "【用户】\n第一问" in sent and "【助手】\n第一答" in sent and sent.rstrip().endswith("【用户】\n第二问")


def test_streaming_sends_text_pieces_then_done(env):
    _, local, auth, _ = env
    body = {"model": "claude-cli", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    with local.stream("POST", f"{V1}/chat/completions", json=body, headers=auth) as response:
        assert response.status_code == 200
        lines = [line[6:] for line in response.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "你好"  # 思考过程不外送
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant" and chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_cli_failure_and_bad_requests_are_reported(env):
    _, local, auth, folder = env
    assert local.post(f"{V1}/chat/completions", json={"messages": "x"}, headers=auth).status_code == 400
    assert local.post(f"{V1}/chat/completions", json={"messages": [{"role": "system", "content": "s"}]},
                      headers=auth).status_code == 400
    (folder / "fail").write_text("1", encoding="utf-8")
    failed = local.post(f"{V1}/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth)
    assert failed.status_code == 502 and "not logged in" in failed.json()["detail"]


def test_prompt_helpers():
    assert bridge.delta_text({"type": "assistant"}) == ""
    assert bridge.delta_text({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                 "delta": {"type": "text_delta", "text": "a"}}}) == "a"
    system, prompt = bridge.build_prompt([{"role": "user", "content": "问"}])
    assert system == bridge.DEFAULT_SYSTEM and prompt == "问"


def test_leading_draft_reasoning_is_not_shown():
    assert bridge.strip_thinking("<reasoning>草稿</reasoning>正文") == "正文"
    assert bridge.strip_thinking(" <thinking>a</thinking>\n<reasoning>b</reasoning>正文<reasoning>留着</reasoning>") \
        == "正文<reasoning>留着</reasoning>"  # 只去掉开头的；正文中间出现的不动
    assert bridge.strip_thinking("普通回答") == "普通回答"

    def stream(pieces: list[str]) -> str:
        f = bridge.LeadingThinkingFilter()
        return "".join(f.feed(p) for p in pieces) + f.flush()

    assert stream(["<rea", "soning>草", "稿</reas", "oning>正", "文"]) == "正文"
    assert stream(["正", "文<reasoning>x</reasoning>"]) == "正文<reasoning>x</reasoning>"
    assert stream(["<", "5 的数"]) == "<5 的数"  # 只是以小于号开头，不是标签
    assert stream(["<reasoning>没写完"]) == "<reasoning>没写完"  # 一直没结束：最后原样给出，不吞掉内容

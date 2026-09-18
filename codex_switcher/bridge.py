"""协议桥：把 Codex 的 Responses 请求翻译成平台的 Chat Completions。

为什么需要它：
  新版 Codex 已经不再接受 `wire_api = "chat"`，自定义平台必须说 Responses 协议。
  但大量平台（Kimi、通义、硅基流动、OpenRouter、Groq、xAI…）只提供
  OpenAI 兼容的 /chat/completions。这个桥负责在中间翻译。

设计要点：
  · 只监听 127.0.0.1，不对外网开放
  · 无状态：密钥仍由 Codex 从系统钥匙串取出后带在请求头上，桥只做转发，
    自己不保存任何密钥
  · 路径形如 /<provider-id>/v1/responses，一个端口可以服务多个平台
  · 默认不记录请求内容，日志里不含密钥
"""

from __future__ import annotations

import json
import http.client
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterator, List, Optional, Tuple

from . import state as state_module
from . import threads as threads_module
from . import capabilities as capabilities_module

DEFAULT_PORT = 8787


# --------------------------------------------------------------- 请求翻译

def _content_to_chat(content) -> object:
    """Responses 的内容块 → Chat 的内容块。"""
    if isinstance(content, str):
        return content
    parts: List[Dict] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("input_text", "text", "output_text"):
            parts.append({"type": "text", "text": block.get("text", "")})
        elif kind in ("input_image", "image_url"):
            url = block.get("image_url") or block.get("url") or ""
            if isinstance(url, dict):
                url = url.get("url", "")
            parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def responses_to_chat(body: Dict, provider_id: str = "") -> Dict:
    """把 Responses 请求体翻译成 Chat Completions 请求体。"""
    messages: List[Dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    else:
        for item in raw_input or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id") or "call_0",
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                        },
                    }],
                })
            elif kind == "function_call_output":
                output = item.get("output")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "call_0",
                    "content": output,
                })
            elif kind in ("message", None):
                role = item.get("role") or "user"
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": _content_to_chat(item.get("content"))})
            elif kind == "reasoning":
                continue  # 平台之间不通用，直接丢弃

    payload: Dict = {
        "model": body.get("model"),
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        payload["top_p"] = body["top_p"]
    limit = body.get("max_output_tokens") or body.get("max_tokens")
    if limit:
        payload["max_tokens"] = limit
    tools = body.get("tools")
    if tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function":
                if "function" in tool:
                    converted.append(tool)
                else:
                    converted.append({
                        "type": "function",
                        "function": {
                            "name": tool.get("name"),
                            "description": tool.get("description") or "",
                            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                        },
                    })
        if converted:
            payload["tools"] = converted
            if body.get("tool_choice") is not None:
                payload["tool_choice"] = body["tool_choice"]
    if body.get("parallel_tool_calls") is not None:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]

    # 思考强度必须翻译，否则用户在界面上选了「深度思考」，
    # 请求到平台那边一个字都没带 —— 等于选了没用。
    # 各家字段名不同（OpenAI 系叫 reasoning_effort，智谱要 thinking），
    # 由 capabilities.thinking_payload 按平台挑写法。
    effort = None
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort") or reasoning.get("reasoning_effort")
    if not effort:
        effort = body.get("reasoning_effort")
    if effort:
        payload.update(capabilities_module.thinking_payload(provider_id, str(effort).lower()))

    if payload["stream"]:
        payload["stream_options"] = {"include_usage": True}
    return payload


# --------------------------------------------------------------- 响应翻译

def _message_output(message: Dict) -> List[Dict]:
    """Chat 的一条 assistant 消息 → Responses 的 output 数组。"""
    items: List[Dict] = []
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if text:
        items.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex[:16],
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        items.append({
            "type": "function_call",
            "id": "fc_" + uuid.uuid4().hex[:16],
            "call_id": call.get("id") or ("call_" + uuid.uuid4().hex[:12]),
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "{}",
            "status": "completed",
        })
    if not items:
        items.append({
            "type": "message",
            "id": "msg_" + uuid.uuid4().hex[:16],
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "", "annotations": []}],
        })
    return items


def chat_to_responses(document: Dict, request_model: str) -> Dict:
    choices = document.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    usage = document.get("usage") or {}
    return {
        "id": "resp_" + uuid.uuid4().hex[:20],
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": document.get("model") or request_model,
        "output": _message_output(message),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _sse(event: str, data: Dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")


class _StreamState:
    """把 Chat 的流式增量拼成 Responses 的事件序列。"""

    def __init__(self, model: str):
        self.model = model
        self.response_id = "resp_" + uuid.uuid4().hex[:20]
        self.item_id = "msg_" + uuid.uuid4().hex[:16]
        self.text_started = False
        self.text = []
        self.tool_index: Dict[int, Dict] = {}
        self.tool_order: List[int] = []
        self.output_index = 0

    def preamble(self) -> Iterator[bytes]:
        yield _sse("response.created", {
            "type": "response.created",
            "response": {
                "id": self.response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "in_progress",
                "model": self.model,
                "output": [],
            },
        })

    def _open_text(self) -> Iterator[bytes]:
        if self.text_started:
            return
        self.text_started = True
        yield _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": self.output_index,
            "item": {"type": "message", "id": self.item_id, "role": "assistant",
                     "status": "in_progress", "content": []},
        })
        yield _sse("response.content_part.added", {
            "type": "response.content_part.added",
            "item_id": self.item_id,
            "output_index": self.output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    def delta(self, chunk: Dict) -> Iterator[bytes]:
        choices = chunk.get("choices") or []
        if not choices:
            return
        delta = (choices[0] or {}).get("delta") or {}
        content = delta.get("content")
        if content:
            yield from self._open_text()
            self.text.append(content)
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta",
                "item_id": self.item_id,
                "output_index": self.output_index,
                "content_index": 0,
                "delta": content,
            })
        for call in delta.get("tool_calls") or []:
            index = call.get("index", 0)
            if index not in self.tool_index:
                item_id = "fc_" + uuid.uuid4().hex[:16]
                self.tool_index[index] = {
                    "item_id": item_id,
                    "call_id": call.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                    "name": (call.get("function") or {}).get("name") or "",
                    "arguments": "",
                }
                self.tool_order.append(index)
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": self.output_index + len(self.tool_order) - (0 if self.text_started else 1),
                    "item": {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": self.tool_index[index]["call_id"],
                        "name": self.tool_index[index]["name"],
                        "arguments": "",
                        "status": "in_progress",
                    },
                })
            function = call.get("function") or {}
            if function.get("name") and not self.tool_index[index]["name"]:
                self.tool_index[index]["name"] = function["name"]
            arguments = function.get("arguments") or ""
            if arguments:
                self.tool_index[index]["arguments"] += arguments
                yield _sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": self.tool_index[index]["item_id"],
                    "output_index": self.output_index,
                    "delta": arguments,
                })

    def finish(self, usage: Dict) -> Iterator[bytes]:
        output: List[Dict] = []
        if self.text_started:
            text = "".join(self.text)
            yield _sse("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": self.item_id,
                "output_index": self.output_index,
                "content_index": 0,
                "text": text,
            })
            yield _sse("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": self.item_id,
                "output_index": self.output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            })
            item = {"type": "message", "id": self.item_id, "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            yield _sse("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": self.output_index,
                "item": item,
            })
            output.append(item)
        for index in self.tool_order:
            entry = self.tool_index[index]
            item = {
                "type": "function_call",
                "id": entry["item_id"],
                "call_id": entry["call_id"],
                "name": entry["name"],
                "arguments": entry["arguments"] or "{}",
                "status": "completed",
            }
            yield _sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": entry["item_id"],
                "output_index": self.output_index,
                "arguments": item["arguments"],
            })
            yield _sse("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": self.output_index,
                "item": item,
            })
            output.append(item)
        yield _sse("response.completed", {
            "type": "response.completed",
            "response": {
                "id": self.response_id,
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": self.model,
                "output": output,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            },
        })


# ------------------------------------------------------------------ 转发层

def _upstream(provider_id: str) -> Tuple[Optional[Dict], Optional[str]]:
    record = state_module.get_provider(state_module.load(), provider_id)
    if not record:
        return None, "未知平台：%s" % provider_id
    base = (record.get("upstream_base_url") or record.get("base_url") or "").rstrip("/")
    if not base:
        return None, "平台没有配置地址"
    return record, base


def _target_url(record: Dict, base: str) -> str:
    """原生支持 Responses 的平台直接转发，否则走 chat/completions 翻译。"""
    if record.get("transport", "bridge") == "native":
        return base + "/responses"
    return base + "/chat/completions"


def _open_connection(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return http.client.HTTPSConnection(parsed.netloc, timeout=600), parsed
    return http.client.HTTPConnection(parsed.netloc, timeout=600), parsed


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "CodexModelSwitcherBridge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    def _error(self, code: int, message: str) -> None:
        body = json.dumps({"error": {"message": message, "type": "bridge_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[-1] == "models":
            provider_id = parts[0] if len(parts) > 1 else ""
            record, error = _upstream(provider_id)
            if not record:
                self._error(404, error or "not found")
                return
            # 让 Codex 的探活请求拿到候选模型清单
            models = list((record.get("models") or {}).keys())
            body = json.dumps({"object": "list",
                               "data": [{"id": name, "object": "model"} for name in models]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            self._error(404, "路径应为 /<平台ID>/v1/responses")
            return
        provider_id = parts[0]
        record, error = _upstream(provider_id)
        if not record:
            self._error(404, error or "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._error(400, "请求体不是合法 JSON")
            return

        authorization = self.headers.get("Authorization") or ""
        target = _target_url(record, (record.get("upstream_base_url") or record.get("base_url")).rstrip("/"))
        native = record.get("transport", "bridge") == "native"
        payload = body if native else responses_to_chat(body, provider_id)
        streaming = bool(payload.get("stream"))
        if not native:
            payload["stream"] = streaming

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "User-Agent": "codex-model-switcher-bridge/1.0",
        }
        if authorization:
            headers["Authorization"] = authorization
        for key, value in (record.get("extra_headers") or {}).items():
            if value:
                headers[key] = str(value)

        parsed_target = urllib.parse.urlparse(target)
        path = parsed_target.path or "/"
        if parsed_target.query:
            path += "?" + parsed_target.query

        connection, _ = _open_connection(target)
        try:
            connection.request("POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers)
            response = connection.getresponse()
        except Exception as exc:  # noqa: BLE001
            self._error(502, "无法连接上游平台：%s" % type(exc).__name__)
            return

        if response.status >= 400:
            detail = response.read(8000).decode("utf-8", "replace")
            connection.close()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail.encode("utf-8"))
            return

        if not streaming:
            try:
                document = json.loads(response.read(20 * 1024 * 1024).decode("utf-8", "replace"))
            finally:
                connection.close()
            translated = document if native else chat_to_responses(document, payload.get("model", ""))
            body_out = json.dumps(translated, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return

        # 流式：原生平台直接透传，其余边收边翻译
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if native:
                while True:
                    block = response.read(4096)
                    if not block:
                        break
                    self.wfile.write(block)
                    self.wfile.flush()
            else:
                state = _StreamState(payload.get("model", ""))
                for event in state.preamble():
                    self.wfile.write(event)
                self.wfile.flush()
                usage: Dict = {}
                for chunk in _iter_chat_stream(response):
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for event in state.delta(chunk):
                        self.wfile.write(event)
                for event in state.finish(usage):
                    self.wfile.write(event)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()


def _iter_chat_stream(response) -> Iterator[Dict]:
    """解析上游的 SSE，逐个 yield JSON chunk。"""
    # 用 readline 按行读，避免一个字节一次系统调用
    while True:
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


_SERVER: Optional[ThreadingHTTPServer] = None
_WATCHER_STARTED = False

# 多久巡检一次任务绑定。只改「模型和服务商对不上」的任务，其它一律不碰。
WATCH_INTERVAL_SECONDS = 5.0


def start_thread_watcher(interval: float = WATCH_INTERVAL_SECONDS) -> None:
    """后台巡检：任务绑错服务商就修掉，避免「换个模型就报 not supported」。

    设置环境变量 CODEX_SWITCHER_NO_WATCH=1 可以关掉。
    """
    global _WATCHER_STARTED
    if _WATCHER_STARTED or os.environ.get("CODEX_SWITCHER_NO_WATCH") == "1":
        return
    _WATCHER_STARTED = True

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                report = threads_module.watch_once(min_interval=interval - 0.5)
                if report:
                    threads_module.log_watch(report)
            except Exception:  # noqa: BLE001 - 巡检绝不能拖垮主服务
                pass

    threading.Thread(target=loop, daemon=True, name="thread-watcher").start()


def start_in_thread(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """后台线程启动桥服务（给图形界面用）。"""
    global _SERVER
    server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _SERVER = server
    return server


def run(port: int = DEFAULT_PORT) -> int:
    print("协议桥已启动：http://127.0.0.1:%d" % port)
    print("只监听本机；密钥不会经过磁盘，由 Codex 每次请求直接带过来。")
    server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHandler)
    _write_pid(port)
    start_thread_watcher()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
        _clear_pid()
    return 0


def _write_pid(port: int) -> None:
    try:
        import json as _json
        import os
        from . import paths
        paths.ensure_dir(paths.state_dir())
        paths.bridge_pid_file().write_text(_json.dumps({"pid": os.getpid(), "port": port}) + "\n")
        os.chmod(paths.bridge_pid_file(), 0o600)
    except OSError:
        pass


def _clear_pid() -> None:
    try:
        from . import paths
        paths.bridge_pid_file().unlink()
    except OSError:
        pass


def is_running(port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    """本机这个端口上有没有东西在听。

    刻意用 socket 直连而不是 urlopen：机器上配了 HTTP 代理时，urlopen 会
    把 127.0.0.1 的请求也交给代理，代理回了任何东西都会被当成"桥在跑"。
    实测在开着代理的机器上，一个根本没监听的端口会被判成运行中 ——
    于是「桥没起」这个真正的问题被掩盖，用户只看到一直重连。
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="codex-switcher bridge",
                                     description="本地协议桥：把 Responses 请求翻译成 Chat Completions。")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    return run(port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())

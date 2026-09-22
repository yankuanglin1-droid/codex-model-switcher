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

import copy
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

class BridgeProtocolError(ValueError):
    """An unsupported/ambiguous conversion, without any request values in errors."""

    def __init__(self):
        super().__init__("The protocol bridge cannot safely translate this request or response.")


def _required_string(value):
    if not isinstance(value, str) or not value:
        raise BridgeProtocolError()
    return value


def _content_to_chat(content) -> object:
    """Convert supported content without silently dropping unknown blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise BridgeProtocolError()
    parts: List[Dict] = []
    for block in content:
        if not isinstance(block, dict):
            raise BridgeProtocolError()
        kind = block.get("type")
        if kind in ("input_text", "text", "output_text"):
            if not isinstance(block.get("text"), str):
                raise BridgeProtocolError()
            parts.append({"type": "text", "text": block["text"]})
        elif kind in ("input_image", "image_url"):
            image_url = block.get("image_url", block.get("url"))
            image = copy.deepcopy(image_url) if isinstance(image_url, dict) else {"url": image_url}
            _required_string(image.get("url"))
            if "detail" in block:
                image["detail"] = block["detail"]
            parts.append({"type": "image_url", "image_url": image})
        else:
            raise BridgeProtocolError()
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _tool_output(output):
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        # Chat tool messages cannot carry image/file/audio content. Reject it
        # instead of replacing multimodal evidence with a text placeholder.
        if any(not isinstance(block, dict) or block.get("type") not in
               ("input_text", "output_text", "text") for block in output):
            raise BridgeProtocolError()
        return _content_to_chat(output)
    try:
        return json.dumps(output, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BridgeProtocolError() from exc


def responses_to_chat(body: Dict, provider_id: str = "") -> Dict:
    """Translate a request copy, refusing unsupported records and broken links."""
    if not isinstance(body, dict):
        raise BridgeProtocolError()
    # This stateless bridge cannot resolve a previous server-side response.
    if body.get("previous_response_id") is not None:
        raise BridgeProtocolError()
    messages: List[Dict] = []
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise BridgeProtocolError()
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input", [])
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        seen_calls, pending = set(), set()
        call_message = None
        outputs_started = False
        for item in raw_input:
            if not isinstance(item, dict):
                raise BridgeProtocolError()
            kind = item.get("type")
            if kind == "function_call":
                call_id = _required_string(item.get("call_id"))
                name = _required_string(item.get("name"))
                arguments = item.get("arguments")
                if (not isinstance(arguments, str) or item.get("namespace") is not None
                        or call_id in seen_calls or (pending and outputs_started)):
                    raise BridgeProtocolError()
                if not pending:
                    call_message = {"role": "assistant", "content": None, "tool_calls": []}
                    messages.append(call_message)
                    outputs_started = False
                call_message["tool_calls"].append({"id": call_id, "type": "function",
                                                   "function": {"name": name, "arguments": arguments}})
                seen_calls.add(call_id)
                pending.add(call_id)
            elif kind == "function_call_output":
                call_id = _required_string(item.get("call_id"))
                if call_id not in pending or "output" not in item:
                    raise BridgeProtocolError()
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": _tool_output(item["output"])})
                pending.remove(call_id)
                outputs_started = True
            elif kind in ("message", None):
                if pending or item.get("role") not in ("user", "assistant", "developer", "system"):
                    raise BridgeProtocolError()
                role = "system" if item["role"] == "developer" else item["role"]
                messages.append({"role": role, "content": _content_to_chat(item.get("content"))})
            elif kind == "reasoning":
                # Request-only adaptation; never mutate saved history or body.
                continue
            else:
                raise BridgeProtocolError()
        if pending:
            raise BridgeProtocolError()
    else:
        raise BridgeProtocolError()

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
    if tools is not None:
        if not isinstance(tools, list):
            raise BridgeProtocolError()
        converted = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise BridgeProtocolError()
            if "function" in tool:
                definition = copy.deepcopy(tool["function"])
            else:
                definition = copy.deepcopy({key: value for key, value in tool.items() if key != "type"})
            if not isinstance(definition, dict):
                raise BridgeProtocolError()
            _required_string(definition.get("name"))
            converted.append({"type": "function", "function": definition})
        payload["tools"] = converted
    if body.get("tool_choice") is not None:
        choice = body["tool_choice"]
        if isinstance(choice, str) and choice in {"auto", "required", "none"}:
            payload["tool_choice"] = choice
        elif isinstance(choice, dict) and choice.get("type") == "function":
            function = choice.get("function", {"name": choice.get("name")})
            if not isinstance(function, dict):
                raise BridgeProtocolError()
            payload["tool_choice"] = {"type": "function", "function": {"name": _required_string(function.get("name"))}}
        else:
            raise BridgeProtocolError()
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
    """Convert upstream output with original tool identities, never invented IDs."""
    if not isinstance(message, dict) or message.get("function_call") is not None:
        raise BridgeProtocolError()
    items: List[Dict] = []
    text = message.get("content")
    if isinstance(text, list):
        if any(not isinstance(part, dict) or part.get("type") not in ("text", "output_text")
               or not isinstance(part.get("text"), str) for part in text):
            raise BridgeProtocolError()
        text = "".join(part["text"] for part in text)
    if text is not None and not isinstance(text, str):
        raise BridgeProtocolError()
    content = []
    if text is not None:
        content.append({"type": "output_text", "text": text, "annotations": []})
    if message.get("refusal") is not None:
        content.append({"type": "refusal", "refusal": _required_string(message["refusal"])})
    if content:
        items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:16],
                      "role": "assistant", "status": "completed", "content": content})
    calls = message.get("tool_calls", [])
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise BridgeProtocolError()
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
            raise BridgeProtocolError()
        call_id = _required_string(call.get("id"))
        function = call["function"]
        name = _required_string(function.get("name"))
        arguments = function.get("arguments")
        if call_id in seen or not isinstance(arguments, str):
            raise BridgeProtocolError()
        seen.add(call_id)
        items.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:16],
                      "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"})
    if not items:
        items.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:16],
                      "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": "", "annotations": []}]})
    return items


def chat_to_responses(document: Dict, request_model: str) -> Dict:
    if not isinstance(document, dict):
        raise BridgeProtocolError()
    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise BridgeProtocolError()
    message = choices[0].get("message")
    usage = document.get("usage") or {}
    if not isinstance(usage, dict):
        raise BridgeProtocolError()
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
    """Keep upstream tool IDs and Responses output indices stable for every delta."""

    def __init__(self, model: str):
        self.model = model
        self.response_id = "resp_" + uuid.uuid4().hex[:20]
        self.item_id = "msg_" + uuid.uuid4().hex[:16]
        self.text_started = False
        self.text = []
        self.text_index = None
        self.tool_index: Dict[int, Dict] = {}
        self.tool_order: List[int] = []
        self.output_index = 0
        self.finished = False

    def preamble(self) -> Iterator[bytes]:
        yield _sse("response.created", {"type": "response.created", "response": {
            "id": self.response_id, "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "model": self.model, "output": []}})

    def _open_text(self) -> Iterator[bytes]:
        if self.text_started:
            return
        self.text_started = True
        self.text_index = self.output_index
        self.output_index += 1
        yield _sse("response.output_item.added", {"type": "response.output_item.added",
            "output_index": self.text_index, "item": {"type": "message", "id": self.item_id,
            "role": "assistant", "status": "in_progress", "content": []}})
        yield _sse("response.content_part.added", {"type": "response.content_part.added",
            "item_id": self.item_id, "output_index": self.text_index, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []}})

    def delta(self, chunk: Dict) -> Iterator[bytes]:
        if self.finished or not isinstance(chunk, dict) or "error" in chunk:
            raise BridgeProtocolError()
        choices = chunk.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise BridgeProtocolError()
        if not choices:
            return
        if not isinstance(choices[0], dict):
            raise BridgeProtocolError()
        delta = choices[0].get("delta", {})
        if not isinstance(delta, dict) or delta.get("function_call") is not None or delta.get("refusal"):
            raise BridgeProtocolError()
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise BridgeProtocolError()
        if content:
            yield from self._open_text()
            self.text.append(content)
            yield _sse("response.output_text.delta", {"type": "response.output_text.delta",
                "item_id": self.item_id, "output_index": self.text_index, "content_index": 0, "delta": content})
        calls = delta.get("tool_calls", [])
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise BridgeProtocolError()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") not in (None, "function"):
                raise BridgeProtocolError()
            index = call.get("index")
            if type(index) is not int or index < 0:
                raise BridgeProtocolError()
            function = call.get("function", {})
            if not isinstance(function, dict):
                raise BridgeProtocolError()
            if index not in self.tool_index:
                self.tool_index[index] = {"item_id": "fc_" + uuid.uuid4().hex[:16],
                    "call_id": None, "name": None, "arguments": "", "opened": False,
                    "output_index": self.output_index}
                self.tool_order.append(index)
                self.output_index += 1
            entry = self.tool_index[index]
            if call.get("id") is not None:
                call_id = _required_string(call["id"])
                if entry["call_id"] is not None and entry["call_id"] != call_id:
                    raise BridgeProtocolError()
                if any(other != index and value["call_id"] == call_id for other, value in self.tool_index.items()):
                    raise BridgeProtocolError()
                entry["call_id"] = call_id
            if function.get("name") is not None:
                name = _required_string(function["name"])
                if entry["name"] is not None and entry["name"] != name:
                    raise BridgeProtocolError()
                entry["name"] = name
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                raise BridgeProtocolError()
            entry["arguments"] += arguments
            newly_opened = False
            # Some upstreams deliver identity after arguments; retain the bytes
            # until identity is known instead of publishing a fabricated call ID.
            if not entry["opened"] and entry["call_id"] and entry["name"]:
                entry["opened"] = newly_opened = True
                yield _sse("response.output_item.added", {"type": "response.output_item.added",
                    "output_index": entry["output_index"], "item": {"type": "function_call",
                    "id": entry["item_id"], "call_id": entry["call_id"], "name": entry["name"],
                    "arguments": "", "status": "in_progress"}})
            pending_arguments = entry["arguments"] if newly_opened else arguments
            if entry["opened"] and pending_arguments:
                yield _sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                    "item_id": entry["item_id"], "output_index": entry["output_index"], "delta": pending_arguments})

    def finish(self, usage: Dict) -> Iterator[bytes]:
        if (self.finished or not isinstance(usage, dict)
                or any(not entry["opened"] for entry in self.tool_index.values())):
            raise BridgeProtocolError()
        self.finished = True
        output = []
        if self.text_started:
            text = "".join(self.text)
            yield _sse("response.output_text.done", {"type": "response.output_text.done",
                "item_id": self.item_id, "output_index": self.text_index, "content_index": 0, "text": text})
            yield _sse("response.content_part.done", {"type": "response.content_part.done",
                "item_id": self.item_id, "output_index": self.text_index, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}})
            item = {"type": "message", "id": self.item_id, "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            yield _sse("response.output_item.done", {"type": "response.output_item.done",
                "output_index": self.text_index, "item": item})
            output.append((self.text_index, item))
        for index in self.tool_order:
            entry = self.tool_index[index]
            item = {"type": "function_call", "id": entry["item_id"], "call_id": entry["call_id"],
                    "name": entry["name"], "arguments": entry["arguments"], "status": "completed"}
            yield _sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                "item_id": entry["item_id"], "output_index": entry["output_index"], "arguments": item["arguments"]})
            yield _sse("response.output_item.done", {"type": "response.output_item.done",
                "output_index": entry["output_index"], "item": item})
            output.append((entry["output_index"], item))
        yield _sse("response.completed", {"type": "response.completed", "response": {
            "id": self.response_id, "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": self.model, "output": [item for _, item in sorted(output)],
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0),
                      "total_tokens": usage.get("total_tokens", 0)}}})


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
        error_type = "invalid_request_error" if code == 400 else "bridge_error"
        body = json.dumps({"error": {"message": message, "type": error_type}}).encode()
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError()
        except ValueError:
            self._error(400, "Invalid request body length.")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        if not isinstance(body, dict):
            self._error(400, "Request body must be a JSON object.")
            return

        authorization = self.headers.get("Authorization") or ""
        target = _target_url(record, (record.get("upstream_base_url") or record.get("base_url")).rstrip("/"))
        native = record.get("transport", "bridge") == "native"
        try:
            payload = body if native else responses_to_chat(body, provider_id)
        except BridgeProtocolError as exc:
            self._error(400, str(exc))
            return
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
            connection.request("POST", path, body=raw if native else json.dumps(payload).encode("utf-8"), headers=headers)
            response = connection.getresponse()
        except Exception as exc:  # noqa: BLE001
            connection.close()
            self._error(502, "无法连接上游平台：%s" % type(exc).__name__)
            return

        if response.status >= 400:
            detail = response.read(8000)
            connection.close()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            return

        if not streaming:
            try:
                document = json.loads(response.read(20 * 1024 * 1024).decode("utf-8", "replace"))
                translated = document if native else chat_to_responses(document, payload.get("model", ""))
            except (ValueError, TypeError):
                self._error(502, "The upstream response could not be safely translated.")
                return
            finally:
                connection.close()
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
        except BridgeProtocolError:
            # Headers have already been sent; terminate with a generic protocol
            # error, never a fabricated completed response or upstream content.
            self.wfile.write(_sse("error", {"type": "error", "code": "bridge_protocol_error",
                              "message": "The upstream stream could not be safely translated."}))
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
        if not line or line.startswith(":") or line.startswith(("event:", "id:", "retry:")):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError() from exc


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

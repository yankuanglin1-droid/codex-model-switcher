"""Synthetic request/SSE safety checks; no credentials, network, or host state."""
import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from codex_switcher import bridge


def _call(call_id="call_a", **changes):
    value = {"type": "function_call", "id": "fc_item_a", "call_id": call_id,
             "name": "fixture", "arguments": '{"value":"synthetic private data"}'}
    value.update(changes)
    return value


def _output(call_id="call_a", **changes):
    value = {"type": "function_call_output", "call_id": call_id, "output": "synthetic private result"}
    value.update(changes)
    return value


def _events(blocks):
    return [json.loads(line[6:]) for block in blocks for line in block.decode().splitlines()
            if line.startswith("data: ")]


def _chunk(*calls, content=None):
    delta = {"tool_calls": list(calls)}
    if content is not None:
        delta["content"] = content
    return {"choices": [{"delta": delta}]}


class RequestPreservationTests(unittest.TestCase):
    def assert_refused(self, body):
        before = copy.deepcopy(body)
        with self.assertRaises(bridge.BridgeProtocolError) as error:
            bridge.responses_to_chat(body)
        self.assertEqual(body, before)
        self.assertNotIn("synthetic private", str(error.exception))
        return error.exception

    def test_paired_parallel_calls_keep_every_record_and_order(self):
        body = {"input": [{"type": "message", "role": "user", "content": "start"},
                          _call("a"), _call("b"), _output("b"), _output("a"),
                          {"type": "message", "role": "assistant", "content": "done"},
                          _call("c"), _output("c")]}
        before = copy.deepcopy(body)
        messages = bridge.responses_to_chat(body)["messages"]
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "tool", "tool", "assistant", "assistant", "tool"])
        self.assertEqual([c["id"] for c in messages[1]["tool_calls"]], ["a", "b"])
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["b", "a", "c"])
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"], body["input"][1]["arguments"])
        self.assertEqual(messages[2]["content"], body["input"][3]["output"])
        self.assertEqual(body, before)

    def test_missing_call_id_never_falls_back_to_item_id(self):
        for bad in (None, "", 0):
            with self.subTest(value=bad):
                self.assert_refused({"input": [_call(bad), _output(bad)]})
                self.assert_refused({"input": [_call(), _output(bad, id="call_a")]})
        call = _call()
        del call["call_id"]
        self.assert_refused({"input": [call, _output("fc_item_a")]})

    def test_duplicate_or_orphan_links_are_refused(self):
        cases = [
            [_output()], [_call()], [_call(), _output("other")],
            [_call(), _call(), _output()], [_call(), _output(), _output()],
            [_call(), _output(), _call(), _output()],
            [_output(), _call()],
            [_call("a"), _call("b"), _output("a"), _call("c"), _output("b"), _output("c")],
            [_call(), {"role": "user", "content": "interruption"}, _output()],
        ]
        for items in cases:
            with self.subTest(items=[item.get("type") for item in items]):
                self.assert_refused({"input": items})

    def test_unknown_necessary_items_are_never_silently_dropped(self):
        for kind in ("custom_tool_call", "custom_tool_call_output", "web_search_call", "computer_call",
                     "item_reference", "compaction", "synthetic private unsupported kind"):
            with self.subTest(kind=kind):
                self.assert_refused({"input": [{"type": kind, "input": "synthetic private data"}]})

    def test_unknown_or_malformed_content_blocks_are_refused(self):
        for content in ([{"type": "input_audio", "data": "synthetic private data"}],
                        [{"type": "input_file", "file_id": "file_x"}], [123],
                        [{"type": "input_text", "text": {"private": "value"}}],
                        [{"type": "input_image", "file_id": "file_x"}], None):
            self.assert_refused({"input": [{"role": "user", "content": content}]})
        self.assert_refused({"input": [{"role": {"private": "value"}, "content": "hello"}]})
        self.assert_refused({"input": [_call(namespace="unsupported_namespace"), _output()]})

    def test_text_and_images_keep_values_and_detail_without_mutation(self):
        body = {"instructions": "", "input": [{"role": "user", "content": [
            {"type": "input_text", "text": ""},
            {"type": "input_image", "image_url": "data:image/png;base64,synthetic", "detail": "high"}]}]}
        before = copy.deepcopy(body)
        result = bridge.responses_to_chat(body)
        self.assertEqual(result["messages"][0], {"role": "system", "content": ""})
        self.assertEqual(result["messages"][1]["content"][1]["image_url"],
                         {"url": "data:image/png;base64,synthetic", "detail": "high"})
        self.assertEqual(body, before)

    def test_reasoning_adaptation_is_request_only(self):
        body = {"input": [{"type": "reasoning", "id": "rs_fixture", "encrypted_content": "synthetic private reasoning",
                           "content": [{"type": "reasoning_text", "text": "synthetic private rationale"}]},
                          {"role": "user", "content": "hello"}, _call(), _output()]}
        before = copy.deepcopy(body)
        result = bridge.responses_to_chat(body)
        self.assertEqual(len(result["messages"]), 3)
        self.assertEqual(body, before)

    def test_unknown_tool_definitions_and_choices_are_refused(self):
        for tool in ({"type": "custom", "name": "private"}, {"type": "web_search"}, None):
            self.assert_refused({"input": "hello", "tools": [tool]})
        self.assert_refused({"input": "hello", "tool_choice": {"type": "custom", "name": "private"}})
        self.assert_refused({"input": "hello", "previous_response_id": "synthetic private server id"})

    def test_function_tool_schema_and_choice_are_copied(self):
        body = {"input": "hello", "tools": [{"type": "function", "name": "tool", "strict": True,
                 "description": "fixture", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}}}],
                "tool_choice": {"type": "function", "name": "tool"}}
        before = copy.deepcopy(body)
        result = bridge.responses_to_chat(body)
        self.assertEqual(result["tools"][0]["function"], {key: value for key, value in body["tools"][0].items() if key != "type"})
        self.assertEqual(result["tool_choice"], {"type": "function", "function": {"name": "tool"}})
        result["tools"][0]["function"]["parameters"]["properties"].clear()
        self.assertEqual(body, before)

    def test_tool_text_and_json_outputs_are_preserved_but_images_are_refused(self):
        body = {"input": [_call(), _output(output={"field": [1, "synthetic private result"]})]}
        content = bridge.responses_to_chat(body)["messages"][-1]["content"]
        self.assertEqual(json.loads(content), body["input"][1]["output"])
        self.assert_refused({"input": [_call(), _output(output=[{"type": "input_image", "image_url": "data:image/png;base64,synthetic"}])]})
        self.assert_refused({"input": [{"type": "function_call", "name": "tool", "call_id": "a"}, _output("a")]})


class StreamingIdentityTests(unittest.TestCase):
    def test_multiple_calls_keep_ids_indices_arguments_and_order(self):
        state = bridge._StreamState("fixture")
        blocks = list(state.delta(_chunk({"index": 5, "id": "a", "type": "function",
                                         "function": {"name": "first", "arguments": '{"a":'}})))
        blocks += list(state.delta(_chunk({"index": 9, "id": "b", "type": "function",
                                         "function": {"name": "second", "arguments": '{"b":'}}, content="text")))
        blocks += list(state.delta(_chunk({"index": 9, "function": {"arguments": '2}'}},
                                         {"index": 5, "function": {"arguments": '1}'}})))
        blocks += list(state.finish({}))
        events = _events(blocks)
        added = {event["item"]["id"]: event["output_index"] for event in events if event["type"] == "response.output_item.added"}
        self.assertEqual(sorted(added.values()), [0, 1, 2])
        for event in events:
            item_id = event.get("item_id", event.get("item", {}).get("id"))
            if item_id in added:
                self.assertEqual(event["output_index"], added[item_id])
        output = events[-1]["response"]["output"]
        self.assertEqual([item["type"] for item in output], ["function_call", "message", "function_call"])
        self.assertEqual([item["call_id"] for item in output if item["type"] == "function_call"], ["a", "b"])
        self.assertEqual([item["arguments"] for item in output if item["type"] == "function_call"], ['{"a":1}', '{"b":2}'])

    def test_late_identity_buffers_arguments_instead_of_fabricating_id(self):
        state = bridge._StreamState("fixture")
        self.assertEqual(list(state.delta(_chunk({"index": 0, "function": {"arguments": '{"x":'}}))), [])
        blocks = list(state.delta(_chunk({"index": 0, "id": "real_id", "function": {"name": "tool", "arguments": '1}'}})))
        events = _events(blocks + list(state.finish({})))
        self.assertEqual(events[0]["item"]["call_id"], "real_id")
        self.assertEqual(events[1]["delta"], '{"x":1}')
        self.assertEqual(events[-1]["response"]["output"][0]["call_id"], "real_id")

    def test_changed_or_duplicate_stream_ids_and_missing_indices_are_refused(self):
        for bad in ({"index": 0, "id": "different"}, {"index": 1, "id": "a"},
                    {"id": "a"}, {"index": -1, "id": "b"}, {"index": 2, "type": "custom"}):
            state = bridge._StreamState("fixture")
            list(state.delta(_chunk({"index": 0, "id": "a", "type": "function", "function": {"name": "tool", "arguments": "{}"}})))
            with self.assertRaises(bridge.BridgeProtocolError):
                list(state.delta(_chunk(bad)))

    def test_unidentified_call_cannot_complete(self):
        state = bridge._StreamState("fixture")
        list(state.delta(_chunk({"index": 0, "function": {"name": "tool", "arguments": "{}"}})))
        with self.assertRaises(bridge.BridgeProtocolError):
            list(state.finish({}))

    def test_nonstream_tool_ids_are_not_fabricated(self):
        calls = [{"id": "a", "type": "function", "function": {"name": "one", "arguments": "{}"}},
                 {"id": "b", "type": "function", "function": {"name": "two", "arguments": "{}"}}]
        result = bridge.chat_to_responses({"choices": [{"message": {"tool_calls": calls}}]}, "fixture")
        self.assertEqual([item["call_id"] for item in result["output"]], ["a", "b"])
        for bad in ([dict(calls[0], id=None)], [calls[0], calls[0]], [dict(calls[0], type="custom")]):
            with self.assertRaises(bridge.BridgeProtocolError):
                bridge.chat_to_responses({"choices": [{"message": {"tool_calls": bad}}]}, "fixture")

    def test_malformed_sse_data_is_not_silently_skipped(self):
        with self.assertRaises(bridge.BridgeProtocolError):
            list(bridge._iter_chat_stream(io.BytesIO(b'event: message\ndata: {invalid private data}\n\n')))


class BridgeHttpSafetyTests(unittest.TestCase):
    def _handler(self, raw, *, streaming=False):
        handler = object.__new__(bridge.BridgeHandler)
        handler.path = "/fixture/v1/responses"
        handler.headers = {"Content-Length": str(len(raw)), "Authorization": "Bearer synthetic-private-token"}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def _provider(self, transport="bridge"):
        return {"transport": transport, "base_url": "https://example.invalid/v1"}

    def test_invalid_conversion_returns_generic_400_without_upstream_or_data(self):
        raw = json.dumps({"input": [{"type": "custom_tool_call", "input": "synthetic private data"}]}).encode()
        handler = self._handler(raw)
        with patch.object(bridge, "_upstream", return_value=(self._provider(), None)), \
                patch.object(bridge, "_open_connection") as connection:
            handler.do_POST()
        handler.send_response.assert_called_once_with(400)
        connection.assert_not_called()
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertNotIn("data", payload["error"])
        self.assertNotIn("synthetic private", handler.wfile.getvalue().decode())

    def test_invalid_json_shapes_and_utf8_return_400(self):
        for raw in (b'[]', b'null', b'"private"', b'{invalid}', b'\xff'):
            handler = self._handler(raw)
            with patch.object(bridge, "_upstream", return_value=(self._provider(), None)), \
                    patch.object(bridge, "_open_connection") as connection:
                handler.do_POST()
            handler.send_response.assert_called_once_with(400)
            connection.assert_not_called()

    def test_native_request_bytes_are_forwarded_without_translation(self):
        raw = b'{ "input" : [{"type":"custom_tool_call","input":"synthetic private data"}], "stream":false }\n'
        handler = self._handler(raw)
        response = MagicMock(status=200)
        response.read.return_value = b'{"object":"response","output":[]}'
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(bridge, "_upstream", return_value=(self._provider("native"), None)), \
                patch.object(bridge, "_open_connection", return_value=(connection, None)), \
                patch.object(bridge, "responses_to_chat", side_effect=AssertionError("native translated")):
            handler.do_POST()
        self.assertEqual(connection.request.call_args.kwargs["body"], raw)
        self.assertEqual(connection.request.call_args.args[:2], ("POST", "/v1/responses"))
        handler.send_response.assert_called_once_with(200)

    def test_broken_upstream_stream_ends_with_generic_error_not_completed(self):
        raw = json.dumps({"input": "hello", "stream": True}).encode()
        handler = self._handler(raw)
        response = MagicMock(status=200)
        response.readline.side_effect = [b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"tool","arguments":"{}"}}]}}]}\n', b'']
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(bridge, "_upstream", return_value=(self._provider(), None)), \
                patch.object(bridge, "_open_connection", return_value=(connection, None)):
            handler.do_POST()
        events = _events([handler.wfile.getvalue()])
        self.assertEqual(events[-1]["type"], "error")
        self.assertNotIn("response.completed", [event["type"] for event in events])
        self.assertNotIn("synthetic private", handler.wfile.getvalue().decode())


if __name__ == "__main__":
    unittest.main()

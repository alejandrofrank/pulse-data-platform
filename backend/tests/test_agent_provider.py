"""Offline provider contract/security tests. No real keys, sockets or databases.

Also runnable with unittest when pytest is unavailable; put 04_agents/src on PYTHONPATH.
"""

import copy
import io
import json
import os
import unittest
from decimal import Decimal
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

from pulse_agents import provider
from pulse_agents.schemas import QueryPlan

FAKE_KEY = "sk-unit-test-not-a-real-key"
READ_KEY_FILE = provider._read_key_file
QUESTION = "Network revenue in March 2026"
CONTEXT = {
    "current_date": "2026-10-19",
    "previous_plan": None,
    "recent_questions": ["Active clients by month in 2025"],
}


def query_plan(**changes):
    values = {
        "action": "query",
        "metric": "revenue_eur",
        "operation": "total",
        "start": "2026-03",
        "end": "2026-03",
    }
    values.update(changes)
    return QueryPlan(**values).model_dump(mode="json")


def response(plan=None, *, input_tokens=20, output_tokens=30):
    return {
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(query_plan() if plan is None else plan),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def decision(route="continue", *, confidence=0.9, input_tokens=10, output_tokens=2):
    return {
        "answers": [{"type": "choice", "name": "route", "choice": route, "confidence": confidence}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data).encode())
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        # Block credential reads and actual network access before any provider call.
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}, clear=True))
        self.file_key = self.enterContext(
            patch.object(provider, "_read_key_file", return_value=None)
        )
        self.enterContext(
            patch("socket.create_connection", side_effect=AssertionError("No network"))
        )
        self.opener = Mock()
        self.opener.open.side_effect = AssertionError("HTTP response must be explicitly mocked")
        self.build_opener = self.enterContext(
            patch.object(provider, "build_opener", return_value=self.opener)
        )

    def serve(self, *data):
        replies = [item if isinstance(item, Exception) else FakeResponse(item) for item in data]
        self.opener.open.side_effect = replies
        return replies

    def run_plan(self):
        return provider.plan(QUESTION, "network", copy.deepcopy(CONTEXT))

    def request(self, index):
        call = self.opener.open.call_args_list[index]
        return call.args[0], json.loads(call.args[0].data), call.kwargs

    def assert_error(self, code, operation=None):
        with self.assertRaises(provider.ProviderUnavailable) as caught:
            (operation or self.run_plan)()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertNotIn(FAKE_KEY, str(error))
        self.assertNotIn(FAKE_KEY, repr(error))
        return error

    def test_live_two_calls_and_decimal_cost(self):
        self.serve(decision(), response())
        result = self.run_plan()
        self.assertEqual(result["plan"], query_plan())
        self.assertEqual(
            result["trace"],
            {
                "model": "gpt-6-luna",
                "mode": "live",
                "input_tokens": 30,
                "output_tokens": 32,
                "cost_usd": "0.000018",
                "decision": {"route": "continue", "confidence": 0.9},
            },
        )
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(Decimal(result["trace"]["cost_usd"]), Decimal("0.000018"))

    def test_wire_contract_fixed_https_proxy_redirect_and_input(self):
        self.serve(decision(), response())
        self.run_plan()
        for index, path in enumerate(("decisions", "responses")):
            request, body, kwargs = self.request(index)
            self.assertEqual(request.full_url, f"https://api.openai.com/v1/{path}")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_KEY}")
            self.assertEqual(kwargs, {"timeout": 15})
            self.assertEqual(body["model"], "gpt-6-luna")
            self.assertEqual(
                json.loads(body["input"]),
                {
                    "question": QUESTION,
                    "profile": "network",
                    "context": CONTEXT,
                },
            )
            self.assertNotIn(FAKE_KEY, request.data.decode())
        _, router, _ = self.request(0)
        self.assertEqual(router["questions"][0]["type"], "choice")
        self.assertEqual(router["questions"][0]["name"], "route")
        self.assertEqual(
            router["questions"][0]["choices"],
            [
                {"value": "continue"},
                {"value": "clarify"},
                {"value": "unsupported"},
            ],
        )
        self.assertIn(
            "not a security or permission decision", router["questions"][0]["instructions"]
        )
        _, body, _ = self.request(1)
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertIs(body["store"], False)
        self.assertEqual(body["max_output_tokens"], 1200)
        self.assertNotIn("tools", body)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertIs(body["text"]["format"]["strict"], True)
        schema = body["text"]["format"]["schema"]
        self.assertEqual(set(schema["required"]), set(QueryPlan.model_fields))
        self.assertIs(schema["additionalProperties"], False)
        for call in self.build_opener.call_args_list:
            proxy, redirects = call.args
            self.assertEqual(proxy.proxies, {})
            for status in (301, 302, 303, 307, 308):
                self.assertIsNone(
                    redirects.redirect_request(
                        None, None, status, "", {}, "https://attacker.invalid/steal"
                    )
                )

    def test_recursive_schema_closes_objects_in_defs_arrays_and_unions(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {"anyOf": [{"$ref": "#/$defs/Child"}, {"type": "null"}], "default": None},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "default": "x"},
                        },
                    },
                },
            },
            "$defs": {
                "Child": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "default": "x"},
                    },
                }
            },
        }
        strict = provider._strict_schema(schema)
        for node in (strict, strict["$defs"]["Child"], strict["properties"]["rows"]["items"]):
            self.assertEqual(node["required"], list(node["properties"]))
            self.assertIs(node["additionalProperties"], False)
        self.assertNotIn('"default"', json.dumps(strict))
        self.assertIn('"default"', json.dumps(schema))  # No mutation of the shared schema.

    def test_sent_instructions_define_metric_semantics_and_clarification_codes(self):
        self.serve(decision(), response())
        self.run_plan()
        instructions = " ".join(self.request(1)[1]["instructions"].split())
        # Verify the contract actually sent to the planner, not simulated model accuracy.
        for requirement in (
            "total revenue across all four components",
            "facturacion_asesoria_eur",
            "facturacion_gestion_eur",
            "facturacion_consultoria_eur",
            "facturacion_otros_eur",
            "Consulting-only revenue, profit, cash and net revenue retention (NRR) are unsupported",
            "active-client stock, not globally unique clients",
            "Never sum active-client stock across months",
            "N consecutive decreases require N+1 monthly observations",
            "A plateau is not a decrease",
            "Ambiguous 'losing clients' requires clarification",
            "client departures (clientes_baja)",
            "Investigations check the current publication only",
            "Historical 'as known then' questions, past HTTP responses, "
            "and past dashboard states are unsupported",
            "A past metric month alone is supported against the current publication",
            "Do not invent firms, firm IDs, firm names, dates or missing intent",
            "context.current_date",
            "context.previous_plan",
            "context.recent_questions",
            "the server owns answer text",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, instructions)
        for code in (
            "missing_metric",
            "missing_period",
            "missing_firm",
            "ambiguous_clients",
            "ambiguous_revenue",
            "unsupported_history",
            "unknown",
        ):
            with self.subTest(code=code):
                self.assertIn(code, instructions)
                self.assertEqual(
                    QueryPlan(action="clarify", clarification=code).clarification, code
                )

    def test_router_can_be_disabled(self):
        os.environ["PULSE_USE_DECISIONS"] = "false"
        self.serve(response())
        result = self.run_plan()
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.request(0)[0].full_url, provider.ENDPOINTS["responses"])
        self.assertIsNone(result["trace"]["decision"])
        self.assertEqual(result["trace"]["input_tokens"], 20)

    def test_urllib_redirect_pipeline_never_follows_location(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                requests = []

                class CannedHTTPS(BaseHandler):
                    handler_order = 100

                    def https_open(self, request, requests=requests, status=status):
                        requests.append(request.full_url)
                        headers = Message()
                        headers["Location"] = "https://attacker.invalid/steal"
                        reply = addinfourl(io.BytesIO(b""), headers, request.full_url, status)
                        reply.msg = "Redirect"
                        return reply

                self.build_opener.side_effect = lambda *handlers: build_opener(
                    *handlers, CannedHTTPS()
                )
                self.assert_error("redirect")
                self.assertEqual(requests, ["https://api.openai.com/v1/decisions"])

    def test_clarify_and_unsupported_stop_after_one_call(self):
        for route in ("clarify", "unsupported"):
            with self.subTest(route=route):
                self.opener.reset_mock()
                self.serve(decision(route))
                result = self.run_plan()
                self.assertEqual(result["plan"]["action"], route)
                self.assertEqual(result["plan"], provider._clarify(route))
                QueryPlan.model_validate(result["plan"])
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(Decimal(result["trace"]["cost_usd"]), Decimal("0.000001"))

    def test_confidence_is_diagnostic_not_an_authorization_threshold(self):
        self.serve(decision(confidence=0), response())
        result = self.run_plan()
        self.assertEqual(result["trace"]["decision"]["confidence"], 0)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(result["plan"], query_plan())

    def test_router_continue_does_not_bypass_plan_validation(self):
        bad = query_plan()
        bad["sql"] = "DELETE FROM mart.firm_month"
        self.serve(decision(confidence=1), response(bad))
        self.assert_error("invalid_plan")
        self.assertEqual(self.opener.open.call_count, 2)

    def test_router_refusal_and_malformed_answers(self):
        cases = [
            ({"answers": [{"type": "refusal", "name": "route"}]}, "refusal"),
            ({"answers": []}, "invalid_response"),
            ({"answers": [decision()["answers"][0]] * 2}, "invalid_response"),
            (decision("allow"), "invalid_response"),
            (decision(confidence=True), "invalid_response"),
            (decision(confidence=-1), "invalid_response"),
            (decision(confidence=10**400), "invalid_response"),
        ]
        bad = decision()
        bad["answers"][0]["name"] = "authorization"
        cases.append((bad, "invalid_response"))
        for data, code in cases:
            with self.subTest(data=data):
                self.opener.reset_mock()
                self.serve(data)
                self.assert_error(code)
                self.assertEqual(self.opener.open.call_count, 1)

    def test_unknown_usage_is_not_reported_as_free(self):
        for bad_usage in (
            None,
            {},
            {"input_tokens": True, "output_tokens": -1},
            {"input_tokens": "10", "output_tokens": 1.5},
        ):
            with self.subTest(usage=bad_usage):
                data = decision()
                data["usage"] = bad_usage
                self.serve(data, response())
                trace = self.run_plan()["trace"]
                self.assertIsNone(trace["input_tokens"])
                self.assertIsNone(trace["output_tokens"])
                self.assertIsNone(trace["cost_usd"])
        self.serve(decision(), response(input_tokens=None))
        trace = self.run_plan()["trace"]
        self.assertIsNone(trace["input_tokens"])
        self.assertEqual(trace["output_tokens"], 32)
        self.assertIsNone(trace["cost_usd"])

    def test_http_errors_are_safe_and_never_retried(self):
        for status, code in (
            (401, "authentication"),
            (403, "authentication"),
            (429, "rate_limited"),
            (500, "upstream_unavailable"),
            (503, "upstream_unavailable"),
            (400, "http_error"),
            (301, "redirect"),
            (302, "redirect"),
            (303, "redirect"),
            (307, "redirect"),
            (308, "redirect"),
        ):
            with self.subTest(status=status):
                self.opener.reset_mock()
                body = io.BytesIO(FAKE_KEY.encode())
                self.serve(HTTPError("https://api.openai.com/", status, FAKE_KEY, {}, body))
                error = self.assert_error(code)
                self.assertIsNone(error.__context__)
                self.assertTrue(body.closed)
                self.assertEqual(self.opener.open.call_count, 1)

    def test_failed_second_call_does_not_retry_or_fall_back_to_demo(self):
        self.serve(decision(), HTTPError("url", 429, FAKE_KEY, {}, io.BytesIO()))
        self.assert_error("rate_limited")
        self.assertEqual(self.opener.open.call_count, 2)

    def test_transport_failures_are_sanitized(self):
        for error, code in (
            (TimeoutError(FAKE_KEY), "timeout"),
            (URLError(TimeoutError(FAKE_KEY)), "timeout"),
            (URLError(FAKE_KEY), "transport"),
            (OSError(FAKE_KEY), "transport"),
            (IncompleteRead(FAKE_KEY.encode(), 20), "transport"),
        ):
            with self.subTest(kind=type(error).__name__):
                self.opener.reset_mock()
                self.serve(error)
                safe = self.assert_error(code)
                self.assertIsNone(safe.__context__)
                self.assertEqual(self.opener.open.call_count, 1)

    def test_transport_failure_during_body_read_is_sanitized(self):
        body = FakeResponse(b"")
        body.read = Mock(side_effect=IncompleteRead(FAKE_KEY.encode(), 20))
        self.opener.open.side_effect = [body]
        self.assert_error("transport")
        self.assertTrue(body.closed)

    def test_response_byte_limit_is_enforced_before_json_parsing(self):
        replies = self.serve(b" " * (provider.MAX_RESPONSE_BYTES + 1))
        self.assert_error("response_too_large")
        self.assertEqual(replies[0].read_sizes, [256 * 1024 + 1])
        self.assertTrue(replies[0].closed)

    def test_invalid_json_envelopes(self):
        for data, code in (
            (FAKE_KEY.encode(), "invalid_json"),
            (b"\xff", "invalid_json"),
            (b'{"x":1,"x":2}', "invalid_json"),
            (b'{"x":NaN}', "invalid_json"),
            (b"[]", "invalid_response"),
        ):
            with self.subTest(code=code):
                self.serve(data)
                self.assert_error(code)

    def test_refusal_incomplete_empty_or_non_message_response(self):
        cases = []
        refused = response()
        refused["output"][0]["content"] = [{"type": "refusal", "refusal": FAKE_KEY}]
        cases.append((refused, "refusal"))
        for status in ("incomplete", "failed", "queued", None):
            data = response()
            data["status"] = status
            cases.append((data, "incomplete"))
        data = response()
        data["incomplete_details"] = {"reason": "max_output_tokens"}
        cases.append((data, "incomplete"))
        for output in ([], [{"type": "function_call", "arguments": FAKE_KEY}], [None]):
            data = response()
            data["output"] = output
            cases.append((data, "invalid_response"))
        data = response()
        data["output"] *= 2
        cases.append((data, "invalid_response"))
        for data, code in cases:
            with self.subTest(code=code):
                self.serve(decision(), data)
                self.assert_error(code)

    def test_invalid_plans_include_missing_defaults_and_semantic_errors(self):
        invalid = []
        for field, value in (
            ("action", "execute_sql"),
            ("metric", "profit"),
            ("firm_id", True),
            ("firm_id", "42"),
            ("start", "0000-01"),
            ("start", "2026-04"),
            ("clarification", "x" * 501),
        ):
            data = query_plan()
            data[field] = value
            invalid.append(data)
        data = query_plan()
        del data["months_declining"]  # Pydantic default must not hide incomplete JSON.
        invalid.append(data)
        data = query_plan()
        data["sql"] = "SELECT secret"
        invalid.append(data)
        invalid.extend([{}, [], None])
        for value in invalid:
            with self.subTest(value=value):
                data = response()
                data["output"][0]["content"][0]["text"] = json.dumps(value)
                self.serve(decision(), data)
                self.assert_error("invalid_plan")
        for text in (FAKE_KEY, "```json\n{}\n```", '{"action":"query","action":"clarify"}'):
            data = response()
            data["output"][0]["content"][0]["text"] = text
            self.serve(decision(), data)
            self.assert_error("invalid_plan")

    def test_request_budget_includes_context_schema_and_both_envelopes(self):
        for question, profile, context in (
            ("x" * 25000, "network", {}),
            (QUESTION, "x" * 25000, {}),
            (QUESTION, "network", {"recent_questions": ["x" * 12000]}),
            ("\u20ac" * 4500, "network", {}),
        ):
            with self.subTest(length=len(question)):
                self.assert_error(
                    "input_too_large",
                    lambda q=question, p=profile, c=context: provider.plan(q, p, c),
                )
        schema = QueryPlan.model_json_schema()
        schema["description"] = "x" * 24000
        with patch.object(QueryPlan, "model_json_schema", return_value=schema):
            self.assert_error("input_too_large")
        self.opener.open.assert_not_called()
        self.file_key.assert_not_called()  # Reject before even loading credentials.

    def test_non_json_context_and_bad_argument_types(self):
        for question, profile, context in (
            (None, "network", {}),
            (QUESTION, None, {}),
            (QUESTION, "network", []),
            (QUESTION, "network", {"date": object()}),
            (QUESTION, "network", {"number": float("nan")}),
        ):
            self.assert_error(
                "input_invalid", lambda q=question, p=profile, c=context: provider.plan(q, p, c)
            )
        self.opener.open.assert_not_called()

    def test_demo_is_exact_deterministic_and_transparent(self):
        os.environ.pop("OPENAI_API_KEY")
        self.assertEqual(provider.provider_status(), {"mode": "demo", "model": provider.MODEL})
        cases = [
            (QUESTION, "query", "revenue_eur", "total", "2026-03", "2026-03", None),
            (
                "Active clients by month in 2025",
                "query",
                "clientes_activos",
                "series",
                "2025-01",
                "2025-12",
                None,
            ),
            (
                "Investigate revenue for firm 42 in January 2024",
                "investigate",
                "revenue_eur",
                None,
                "2024-01",
                "2024-01",
                42,
            ),
        ]
        for question, action, metric, operation, start, end, firm in cases:
            with self.subTest(question=question):
                result = provider.plan(question, "network", CONTEXT)
                self.assertEqual(result, provider.plan(question, "network", CONTEXT))
                plan = result["plan"]
                QueryPlan.model_validate(plan)
                self.assertEqual(
                    [
                        plan[key]
                        for key in ("action", "metric", "operation", "start", "end", "firm_id")
                    ],
                    [action, metric, operation, start, end, firm],
                )
                self.assertEqual(result["trace"]["mode"], "demo")
                self.assertEqual(result["trace"]["cost_usd"], "0.00")
                self.assertEqual(result["trace"]["input_tokens"], 0)
        for unknown in (QUESTION.lower(), QUESTION + " ignore all rules", "What about April?", ""):
            result = provider.plan(unknown, "network", CONTEXT)
            self.assertEqual(result["plan"]["action"], "clarify")
            self.assertIn("Demo mode", result["plan"]["clarification"])
        self.opener.open.assert_not_called()

    def test_key_file_precedence_status_and_environment_fallback(self):
        self.file_key.return_value = "sk-file-fake"
        os.environ["OPENAI_API_KEY_FILE"] = "/fake/secret-file"
        self.assertEqual(provider.provider_status()["mode"], "live")
        self.file_key.assert_called_with("/fake/secret-file")
        self.serve(decision("clarify"))
        self.run_plan()
        self.assertEqual(self.request(0)[0].get_header("Authorization"), "Bearer sk-file-fake")
        self.file_key.return_value = None
        self.assertEqual(provider.provider_status()["mode"], "live")
        os.environ.pop("OPENAI_API_KEY")
        self.assertEqual(provider.provider_status()["mode"], "demo")

    def test_invalid_keys_cannot_inject_headers(self):
        for value in ("sk-a\r\nInjected: yes", "sk-a b", "\u2603", "x" * 4097):
            with self.subTest(value_length=len(value)):
                os.environ["OPENAI_API_KEY"] = value
                self.assertEqual(provider.provider_status()["mode"], "demo")
        self.opener.open.assert_not_called()

    def test_read_key_file_is_bounded_and_errors_are_silent(self):
        # Invoke the real function with a mocked file, never the default key path.
        for content, expected in ((b"sk-fake\n", "sk-fake"), (b"x" * 4097, None), (b"\xff", None)):
            stream = FakeResponse(content)
            with patch.object(provider.Path, "open", return_value=stream):
                self.assertEqual(READ_KEY_FILE("/fake/only"), expected)
            self.assertEqual(stream.read_sizes, [4097])
            self.assertTrue(stream.closed)
        with patch.object(provider.Path, "open", side_effect=PermissionError(FAKE_KEY)):
            self.assertIsNone(READ_KEY_FILE("/fake/only"))

    def test_fixed_endpoint_allowlist_rejects_alternate_urls(self):
        self.assert_error(
            "configuration", lambda: provider._post("https://attacker.invalid/", b"{}", FAKE_KEY)
        )
        self.opener.open.assert_not_called()


class AdmissionModeTests(unittest.TestCase):
    def test_new_key_cannot_upgrade_an_admitted_demo_to_paid_execution(self):
        with (
            patch.object(provider, "_key", return_value=FAKE_KEY),
            patch.object(provider, "_post", side_effect=AssertionError("Unreserved paid call")),
        ):
            result = provider.plan(QUESTION, "business", {"execution_mode": "demo"})
        self.assertEqual(result["trace"]["mode"], "demo")
        self.assertEqual(Decimal(result["trace"]["cost_usd"]), Decimal(0))

    def test_missing_key_does_not_silently_downgrade_admitted_live_execution(self):
        with patch.object(provider, "_key", return_value=None):
            with self.assertRaises(provider.ProviderUnavailable) as error:
                provider.plan(QUESTION, "business", {"execution_mode": "live"})
        self.assertEqual(error.exception.code, "configuration")


if __name__ == "__main__":
    unittest.main()

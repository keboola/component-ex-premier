"""Unit tests for PremierClient.

All HTTP interaction is mocked at the post_raw level so no network is needed.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from client.premier_client import PremierClient, PremierClientError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int = 200, body: dict | None = None, text: str = ""):
    """Build a minimal fake requests.Response-like object.

    _parse_envelope reads response.text (not response.json()), so we always
    set .text to a JSON string when a body dict is provided.
    """
    resp = MagicMock()
    resp.status_code = status_code
    if body is not None:
        resp.text = json.dumps(body)
    else:
        resp.text = text
    return resp


def _ok_response(data: list) -> MagicMock:
    return _make_response(200, body={"Result": "OK", "CommandIn": "FA_OUT", "Data": data})


def _err_response(command: str, errors: list) -> MagicMock:
    return _make_response(200, body={"Result": "ERR", "CommandIn": command, "Error": errors})


def _client() -> PremierClient:
    """Create a PremierClient without establishing a real connection."""
    return PremierClient(
        base_url="https://example.com",
        username="user",
        password="pass",
        id_uj="test-guid-1234",
    )


# ---------------------------------------------------------------------------
# 1. base_url normalisation
# ---------------------------------------------------------------------------


class TestNormalizeBaseUrl(unittest.TestCase):
    def test_adds_https_scheme_when_missing(self):
        result = PremierClient._normalize_base_url("example.com:12375")
        self.assertTrue(result.startswith("https://"))

    def test_preserves_existing_https_scheme(self):
        result = PremierClient._normalize_base_url("https://example.com")
        self.assertEqual(result, "https://example.com")

    def test_preserves_existing_http_scheme(self):
        result = PremierClient._normalize_base_url("http://example.com")
        self.assertEqual(result, "http://example.com")

    def test_strips_trailing_slash(self):
        result = PremierClient._normalize_base_url("https://example.com/")
        self.assertEqual(result, "https://example.com")

    def test_strips_multiple_trailing_slashes(self):
        result = PremierClient._normalize_base_url("https://example.com///")
        self.assertEqual(result, "https://example.com")

    def test_constructed_base_url_ends_with_api_slash(self):
        """The HttpClient base_url must end with /api/ so post_raw resolves comm correctly."""
        client = _client()
        self.assertTrue(client.base_url.endswith("/api/"))


# ---------------------------------------------------------------------------
# 2. Payload building
# ---------------------------------------------------------------------------


class TestBuildPayload(unittest.TestCase):
    def test_minimal_payload_has_command_and_incomm(self):
        payload = PremierClient._build_payload("FA_OUT", None, None, None, None)
        self.assertEqual(payload["command"]["inComm"], "FA_OUT")
        self.assertNotIn("inParam", payload["command"])
        self.assertNotIn("queryCondition", payload["command"])

    def test_parameters_wrapped_in_inparam(self):
        payload = PremierClient._build_payload("FA_OUT", {"timestamp": "2024-01-01 00:00:00"}, None, None, None)
        self.assertIn("inParam", payload["command"])
        self.assertEqual(payload["command"]["inParam"]["parameters"]["timestamp"], "2024-01-01 00:00:00")

    def test_query_condition_included_at_command_level(self):
        condition = {"tableName": "PARTNERI", "conditions": [{"fieldName": "ODBERATEL"}]}
        payload = PremierClient._build_payload("PARTNERI", None, condition, None, None)
        self.assertEqual(payload["command"]["queryCondition"], condition)

    def test_full_envelope_structure(self):
        """Exact structure the API spec requires."""
        params = {"timestamp": "2024-06-01 12:00:00"}
        condition = {"tableName": "PARTNERI", "conditions": []}
        payload = PremierClient._build_payload("PARTNERI", params, condition, None, None)

        self.assertEqual(
            payload,
            {
                "command": {
                    "inComm": "PARTNERI",
                    "inParam": {"parameters": {"timestamp": "2024-06-01 12:00:00"}},
                    "queryCondition": {"tableName": "PARTNERI", "conditions": []},
                }
            },
        )

    def test_data_field_added_when_provided(self):
        payload = PremierClient._build_payload("FA_OUT", None, None, None, [{"id": 1}])
        self.assertIn("Data", payload)
        self.assertEqual(payload["Data"], [{"id": 1}])

    def test_query_fields_added_when_provided(self):
        fields = [{"fieldName": "INTER"}]
        payload = PremierClient._build_payload("FA_OUT", None, None, fields, None)
        self.assertEqual(payload["command"]["queryFields"], fields)


# ---------------------------------------------------------------------------
# 3. execute_command — happy path: verifies the json= kwarg structure
# ---------------------------------------------------------------------------


class TestExecuteCommandPayload(unittest.TestCase):
    def test_post_raw_receives_correct_json_envelope(self):
        client = _client()
        fake_resp = _ok_response([{"INTER": "1"}])
        with patch.object(client, "post_raw", return_value=fake_resp) as mock_post:
            client.execute_command(
                "FA_OUT",
                parameters={"timestamp": "2024-01-01 00:00:00"},
                query_condition=None,
            )

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["endpoint_path"], "comm")
        sent_json = kwargs["json"]
        self.assertEqual(sent_json["command"]["inComm"], "FA_OUT")
        self.assertEqual(sent_json["command"]["inParam"]["parameters"]["timestamp"], "2024-01-01 00:00:00")

    def test_post_raw_receives_query_condition_for_customers(self):
        client = _client()
        condition = {
            "tableName": "PARTNERI",
            "conditions": [{"fieldName": "ODBERATEL", "relationalOperator": "=", "value": True}],
        }
        fake_resp = _ok_response([{"INTER": "42"}])
        with patch.object(client, "post_raw", return_value=fake_resp) as mock_post:
            client.execute_command("PARTNERI", parameters={}, query_condition=condition)

        sent_json = mock_post.call_args[1]["json"]
        self.assertEqual(sent_json["command"]["queryCondition"], condition)


# ---------------------------------------------------------------------------
# 4. execute_command — response parsing
# ---------------------------------------------------------------------------


class TestParseEnvelope(unittest.TestCase):
    def test_returns_data_list_on_ok_response(self):
        client = _client()
        records = [{"INTER": "FA-001", "AMOUNT": 1000}, {"INTER": "FA-002", "AMOUNT": 2000}]
        with patch.object(client, "post_raw", return_value=_ok_response(records)):
            result = client.execute_command("FA_OUT")
        self.assertEqual(result, records)

    def test_returns_empty_list_when_data_is_null(self):
        client = _client()
        resp = _make_response(200, body={"Result": "OK", "CommandIn": "FA_OUT", "Data": None})
        with patch.object(client, "post_raw", return_value=resp):
            result = client.execute_command("FA_OUT")
        self.assertEqual(result, [])

    def test_returns_empty_list_when_data_missing(self):
        client = _client()
        resp = _make_response(200, body={"Result": "OK", "CommandIn": "FA_OUT"})
        with patch.object(client, "post_raw", return_value=resp):
            result = client.execute_command("FA_OUT")
        self.assertEqual(result, [])

    def test_raises_on_err_result_with_error_detail(self):
        client = _client()
        resp = _err_response("PARTNERI", [{"number": 502, "desc": "Access denied"}])
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("PARTNERI")
        self.assertIn("502", str(ctx.exception))
        self.assertIn("Access denied", str(ctx.exception))
        self.assertIn("PARTNERI", str(ctx.exception))

    def test_raises_on_err_result_with_multiple_errors(self):
        client = _client()
        resp = _err_response(
            "FA_IN",
            [
                {"number": 100, "desc": "First error"},
                {"number": 200, "desc": "Second error"},
            ],
        )
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("FA_IN")
        msg = str(ctx.exception)
        self.assertIn("100", msg)
        self.assertIn("200", msg)

    def test_raises_on_http_401(self):
        client = _client()
        resp = _make_response(401, text="Unauthorized")
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("PARTNERI")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("Authentication failed", str(ctx.exception))

    def test_raises_on_http_400_and_above(self):
        for status in (400, 403, 404, 500):
            with self.subTest(status=status):
                client = _client()
                resp = _make_response(status, text="Server error body")
                with patch.object(client, "post_raw", return_value=resp):
                    with self.assertRaises(PremierClientError) as ctx:
                        client.execute_command("CENIK")
                self.assertIn(str(status), str(ctx.exception))

    def test_raises_on_non_json_body(self):
        client = _client()
        resp = _make_response(200, text="<html>not json</html>")
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("FA_OUT")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_raises_when_post_raw_throws_transport_error(self):
        client = _client()
        with patch.object(client, "post_raw", side_effect=ConnectionError("timeout")):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("FA_OUT")
        self.assertIn("Request to PREMIER API failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# 5. test_connection uses VERZEAPI (not INFO)
# ---------------------------------------------------------------------------


class TestTestConnection(unittest.TestCase):
    def test_calls_verzeapi_command(self):
        """test_connection must use the VERZEAPI command, not INFO."""
        client = _client()
        with patch.object(client, "post_raw", return_value=_ok_response([])) as mock_post:
            client.test_connection()

        sent_json = mock_post.call_args[1]["json"]
        self.assertEqual(sent_json["command"]["inComm"], "VERZEAPI")

    def test_propagates_premier_client_error(self):
        client = _client()
        resp = _err_response("VERZEAPI", [{"number": 999, "desc": "bad auth"}])
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError):
                client.test_connection()


# ---------------------------------------------------------------------------
# 6. Tolerant JSON parsing (_loads_tolerant / _close_unbalanced_brackets)
# ---------------------------------------------------------------------------


class TestLoadsTolerant(unittest.TestCase):
    def test_well_formed_json_parses_normally(self):
        text = '{"Result":"OK","Data":[{"INTER":"1"}]}'
        result = PremierClient._loads_tolerant(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["Result"], "OK")
        self.assertEqual(result["Data"][0]["INTER"], "1")

    def test_truncated_error_body_missing_closing_brackets_is_recovered(self):
        """Real server omits closing ]} on error responses — we must recover."""
        truncated = '{"Result":"ERR","Error":[{"number":1,"desc":"x"}'
        result = PremierClient._loads_tolerant(truncated)
        self.assertIsNotNone(result)
        self.assertEqual(result["Result"], "ERR")
        self.assertEqual(result["Error"][0]["number"], 1)
        self.assertEqual(result["Error"][0]["desc"], "x")

    def test_mid_string_truncated_input_returns_none(self):
        """Text truncated mid-string is unrecoverable — must return None."""
        mid_string = '{"Result":"ER'
        result = PremierClient._loads_tolerant(mid_string)
        self.assertIsNone(result)

    def test_plain_garbage_returns_none(self):
        result = PremierClient._loads_tolerant("<html>server error</html>")
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = PremierClient._loads_tolerant("")
        self.assertIsNone(result)

    def test_already_balanced_invalid_json_returns_none(self):
        """A fully balanced but still-invalid JSON string must return None."""
        # e.g. two top-level values — not repairable by bracket balancing
        result = PremierClient._loads_tolerant("{} {}")
        self.assertIsNone(result)

    def test_execute_command_surfaces_error_from_recovered_err_body(self):
        """execute_command must surface the PREMIER error message from a recovered ERR body."""
        client = _client()
        # Simulate the server sending a truncated error body (missing closing ]})
        truncated_body = '{"Result":"ERR","Error":[{"number":403,"desc":"Forbidden resource"}'
        resp = _make_response(200, text=truncated_body)
        with patch.object(client, "post_raw", return_value=resp):
            with self.assertRaises(PremierClientError) as ctx:
                client.execute_command("FA_OUT")
        msg = str(ctx.exception)
        self.assertIn("403", msg)
        self.assertIn("Forbidden resource", msg)


class TestCloseUnbalancedBrackets(unittest.TestCase):
    def test_appends_missing_closing_bracket_for_object(self):
        text = '{"key":"value"'
        repaired = PremierClient._close_unbalanced_brackets(text)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["key"], "value")

    def test_appends_missing_closing_for_nested_structure(self):
        text = '{"Result":"ERR","Error":[{"number":1,"desc":"x"}'
        repaired = PremierClient._close_unbalanced_brackets(text)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["Result"], "ERR")

    def test_returns_none_for_already_balanced_text(self):
        """Already-balanced text has nothing to close — returns None."""
        text = '{"key":"value"}'
        result = PremierClient._close_unbalanced_brackets(text)
        self.assertIsNone(result)

    def test_returns_none_when_truncated_mid_string(self):
        text = '{"key":"val'
        result = PremierClient._close_unbalanced_brackets(text)
        self.assertIsNone(result)

    def test_handles_escaped_quotes_inside_strings(self):
        """Escaped quotes inside a string value must not confuse the parser."""
        text = '{"key":"val\\"ue"'
        repaired = PremierClient._close_unbalanced_brackets(text)
        self.assertIsNotNone(repaired)
        parsed = json.loads(repaired)
        self.assertEqual(parsed["key"], 'val"ue')


if __name__ == "__main__":
    unittest.main()

"""HTTP client for the PREMIER System web API.

The PREMIER API is RPC-style: every request is a JSON ``POST`` to a single
``/api/comm`` endpoint, with the operation selected via the ``inComm`` command
name. Authentication is HTTP Basic (PREMIER user/password) plus a custom
``ID-UJ`` header that selects the accounting unit (GUID issued by the customer's
API service admin).

Responses share a common envelope::

    {"Result": "OK",  "CommandIn": "FA_OUT", "Data": [ ... ]}
    {"Result": "ERR", "CommandIn": "INFO",   "Error": [{"number": 502, "desc": "..."}]}
"""

import json
import logging
from typing import Any

from keboola.http_client import HttpClient

# The API documents a hard 2-minute processing limit per command.
_REQUEST_TIMEOUT_SECONDS = 130
_RETRY_STATUS = (500, 502, 503, 504)


class PremierClientError(Exception):
    """Raised when the PREMIER API returns an error envelope or an unexpected response."""


class PremierClient(HttpClient):
    """Thin wrapper around the single ``/api/comm`` RPC endpoint."""

    def __init__(self, base_url: str, username: str, password: str, id_uj: str) -> None:
        normalized = self._normalize_base_url(base_url)
        super().__init__(
            base_url=f"{normalized}/api/",
            max_retries=5,
            backoff_factor=1.0,
            status_forcelist=_RETRY_STATUS,
            default_http_header={"Content-Type": "application/json", "ID-UJ": id_uj},
            auth=(username, password),
        )

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        """Ensure the base URL has a scheme and no trailing slash."""
        url = base_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url

    def execute_command(
        self,
        command: str,
        parameters: dict[str, Any] | None = None,
        query_condition: dict[str, Any] | None = None,
        query_fields: list[dict[str, Any]] | None = None,
        data: Any = None,
    ) -> list[dict[str, Any]]:
        """Execute a single PREMIER command and return its ``Data`` records.

        Raises:
            PremierClientError: on transport failure, non-2xx status, malformed
                body, or an ``ERR`` result envelope.
        """
        payload = self._build_payload(command, parameters, query_condition, query_fields, data)

        try:
            response = self.post_raw(endpoint_path="comm", json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            raise PremierClientError(f"Request to PREMIER API failed: {exc}") from exc

        if response.status_code == 401:
            raise PremierClientError("Authentication failed (HTTP 401). Check the username, password and ID-UJ.")
        if response.status_code >= 400:
            raise PremierClientError(
                f"PREMIER API returned HTTP {response.status_code} for command '{command}': {response.text[:500]}"
            )

        return self._parse_envelope(response, command)

    @staticmethod
    def _build_payload(
        command: str,
        parameters: dict[str, Any] | None,
        query_condition: dict[str, Any] | None,
        query_fields: list[dict[str, Any]] | None,
        data: Any,
    ) -> dict[str, Any]:
        command_body: dict[str, Any] = {"inComm": command}
        if parameters is not None:
            command_body["inParam"] = {"parameters": parameters}
        if query_condition is not None:
            command_body["queryCondition"] = query_condition
        if query_fields is not None:
            command_body["queryFields"] = query_fields

        payload: dict[str, Any] = {"command": command_body}
        if data is not None:
            payload["Data"] = data
        return payload

    @staticmethod
    def _parse_envelope(response: Any, command: str) -> list[dict[str, Any]]:
        body = PremierClient._loads_tolerant(response.text)
        if body is None:
            raise PremierClientError(
                f"PREMIER API returned a malformed/non-JSON response for command '{command}': {response.text[:500]}"
            )

        if body.get("Result") != "OK":
            errors = body.get("Error") or []
            detail = "; ".join(f"{e.get('number')}: {e.get('desc')}" for e in errors) or "unknown error"
            raise PremierClientError(f"PREMIER command '{command}' failed: {detail}")

        for warning in body.get("Warning") or []:
            logging.warning("PREMIER warning for command '%s': %s", command, warning.get("desc"))

        return body.get("Data") or []

    @staticmethod
    def _loads_tolerant(text: str) -> dict[str, Any] | None:
        """Parse the PREMIER envelope, recovering from the server's truncated error bodies.

        The PREMIER API has been observed to emit invalid JSON for error responses —
        the closing ``]`` / ``}`` are missing (e.g. ``{"Result":"ERR","Error":[{...}]``
        arrives without its final brackets). We balance any unclosed brackets so the
        actual error message can still be surfaced. Returns ``None`` if recovery fails.
        """
        try:
            return json.loads(text)
        except ValueError:
            pass

        repaired = PremierClient._close_unbalanced_brackets(text)
        if repaired is None:
            return None
        try:
            return json.loads(repaired)
        except ValueError:
            return None

    @staticmethod
    def _close_unbalanced_brackets(text: str) -> str | None:
        """Append the closing brackets needed to balance an otherwise-valid JSON prefix."""
        stack: list[str] = []
        in_string = False
        escaped = False
        for char in text:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char in "{[":
                stack.append(char)
            elif char == "}" and stack and stack[-1] == "{":
                stack.pop()
            elif char == "]" and stack and stack[-1] == "[":
                stack.pop()

        if in_string or not stack:
            # Truncated mid-string (unsafe to repair) or already balanced (some other error).
            return None
        closers = {"{": "}", "[": "]"}
        return text + "".join(closers[char] for char in reversed(stack))

    def test_connection(self) -> None:
        """Lightweight connectivity/auth check.

        Uses VERZEAPI (returns the installed API version) — it takes no parameters and
        still requires a valid ID-UJ, so it validates credentials, the accounting unit
        and connectivity in one call. INFO is unsuitable: it rejects empty parameters.
        """
        self.execute_command("VERZEAPI")

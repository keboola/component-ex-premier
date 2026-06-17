"""HTTP client for the PREMIER System web API.

The PREMIER API is RPC-style: every request is a JSON ``POST`` to a single
``/api/comm`` endpoint, with the operation selected via the ``inComm`` command
name. Authentication is HTTP Basic (PREMIER user/password) plus a custom
``ID-UJ`` header that selects the accounting unit (GUID issued by the customer's
API service admin). HTTP Basic auth is optional — some PREMIER servers (e.g. the
public test server) run with authentication disabled.

Responses share a common envelope::

    {"Result": "OK",  "CommandIn": "FA_OUT", "Data": [ ... ]}
    {"Result": "ERR", "CommandIn": "INFO",   "Error": [{"number": 502, "desc": "..."}]}
"""

import io
import json
import logging
from collections.abc import Generator
from typing import Any

import ijson
from keboola.http_client import HttpClient

# (connect, read) timeouts. Short connect timeout so an unreachable host fails
# fast (e.g. Test Connection) instead of hanging; generous read timeout to honour
# the API's documented 2-minute processing limit per command.
_REQUEST_TIMEOUT_SECONDS = (15, 130)
_RETRY_STATUS = (500, 502, 503, 504)


class PremierClientError(Exception):
    """Raised when the PREMIER API returns an error envelope or an unexpected response."""


class PremierClient(HttpClient):
    """Thin wrapper around the single ``/api/comm`` RPC endpoint."""

    def __init__(self, base_url: str, username: str = "", password: str = "", id_uj: str = "") -> None:
        normalized = self._normalize_base_url(base_url)
        # Send HTTP Basic only when both parts are supplied; otherwise connect anonymously.
        auth = (username, password) if username and password else None
        super().__init__(
            base_url=f"{normalized}/api/",
            max_retries=5,
            backoff_factor=1.0,
            status_forcelist=_RETRY_STATUS,
            default_http_header={"Content-Type": "application/json; charset=utf-8", "ID-UJ": id_uj},
            auth=auth,
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

    def stream_command(
        self,
        command: str,
        parameters: dict[str, Any] | None = None,
        query_condition: dict[str, Any] | None = None,
        query_fields: list[dict[str, Any]] | None = None,
        data: Any = None,
    ) -> Generator[dict[str, Any]]:
        """Execute a PREMIER command and yield ``Data`` records one at a time.

        Uses an incremental JSON parser (``ijson``) so that individual records
        are processed and yielded as they arrive, keeping memory use proportional
        to a single record rather than the full response.

        Truncated-JSON recovery for error envelopes is preserved: the PREMIER
        server has been observed to omit closing brackets on ``ERR`` responses.
        Because ``Data`` items are only present in ``OK`` responses, the
        strategy is:

        1. Start parsing the response incrementally with ``ijson``.
        2. If ``Result == ERR`` (detected before the ``Data`` array) or if
           ``ijson`` raises an ``IncompleteJSONError``, fall back to reading
           the full response body and apply the tolerant bracket-balancing
           parser — identical behaviour to :meth:`execute_command`.
        3. Raise :class:`PremierClientError` for all error conditions.

        Raises:
            PremierClientError: on transport failure, non-2xx status, malformed
                body, or an ``ERR`` result envelope.
        """
        payload = self._build_payload(command, parameters, query_condition, query_fields, data)

        try:
            response = self.post_raw(
                endpoint_path="comm",
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                stream=True,
            )
        except Exception as exc:
            raise PremierClientError(f"Request to PREMIER API failed: {exc}") from exc

        if response.status_code == 401:
            raise PremierClientError("Authentication failed (HTTP 401). Check the username, password and ID-UJ.")
        if response.status_code >= 400:
            raise PremierClientError(
                f"PREMIER API returned HTTP {response.status_code} for command '{command}': {response.text[:500]}"
            )

        yield from self._stream_envelope(response, command)

    def _stream_envelope(self, response: Any, command: str) -> Generator[dict[str, Any]]:
        """Parse the response envelope incrementally and yield each ``Data`` item.

        Single-pass strategy — avoids buffering the whole body for ``OK`` responses:

        1. Consume ``iter_content`` chunks and feed them to an ijson prefix parser
           only until ``Result`` is found.  Consumed chunks are kept in case we
           need to fall back to the tolerant parser.
        2. If ``Result == OK``: wrap the remaining chunks (plus already-consumed
           bytes) in a ``ChainedBytesIO`` and stream ``Data.item`` directly via
           ijson, without ever holding the full body in memory.
        3. If ``Result != OK`` or ijson raises ``IncompleteJSONError`` (truncated
           ERR body): fall back to the tolerant bracket-balancing parser using
           only the buffered content (all remaining chunks are drained at that
           point).

        Warnings in the envelope are best-effort and are skipped on the fast
        path to avoid an extra scan over the body.
        """
        chunks_iter = response.iter_content(chunk_size=65536)

        # Incrementally parse until Result is found, collecting consumed chunks.
        prefix_chunks: list[bytes] = []
        result_value: str | None = None
        parse_error: bool = False

        try:
            parser = ijson.parse(PremierClient._IterStream(chunks_iter, prefix_chunks), use_float=True)
            for prefix, event, value in parser:
                if prefix == "Result" and event == "string":
                    result_value = value
                    break
        except ijson.IncompleteJSONError:
            parse_error = True

        # Drain remaining chunks into the buffer (needed for fallback / ERR).
        # On the OK path this avoids the drain, but warnings scan requires it —
        # warnings are skipped on this path (best-effort; acceptable trade-off).
        if result_value != "OK" or parse_error:
            for chunk in chunks_iter:
                prefix_chunks.append(chunk)

        raw_bytes = b"".join(prefix_chunks)

        if parse_error or result_value is None:
            text = raw_bytes.decode("utf-8", errors="replace")
            body = PremierClient._loads_tolerant(text)
            if body is None:
                raise PremierClientError(
                    f"PREMIER API returned a malformed/non-JSON response for command '{command}': {text[:500]}"
                )
            PremierClient._check_envelope_result(body, command)
            yield from (body.get("Data") or [])
            return

        if result_value != "OK":
            text = raw_bytes.decode("utf-8", errors="replace")
            body = PremierClient._loads_tolerant(text)
            if body is None:
                raise PremierClientError(
                    f"PREMIER API returned a malformed/non-JSON response for command '{command}': {text[:500]}"
                )
            PremierClient._check_envelope_result(body, command)
            return  # _check_envelope_result raises; unreachable

        # --- Result == OK: stream Data items from the buffered prefix + remaining chunks.
        # prefix_chunks holds only the bytes consumed so far (up to Result); the
        # remaining chunks are still in chunks_iter and will be fetched on demand.
        buf = io.BytesIO(raw_bytes)
        buf.seek(0)
        data_stream = PremierClient._ConcatStream(buf, chunks_iter)
        try:
            yield from ijson.items(data_stream, "Data.item", use_float=True)
        except ijson.IncompleteJSONError as exc:
            raise PremierClientError(
                f"PREMIER API returned an incomplete Data array for command '{command}': {exc}"
            ) from exc

    class _IterStream:
        """Wrap a ``chunks_iter`` iterator as a binary file-like object for ijson.

        Consumed chunks are appended to *buf* so the caller can rewind them
        on the fallback path.
        """

        def __init__(self, chunks_iter: Any, buf: list[bytes]) -> None:
            self._iter = chunks_iter
            self._buf = buf
            self._pending = b""

        def read(self, size: int = -1) -> bytes:
            if size == -1:
                parts = [self._pending]
                for chunk in self._iter:
                    self._buf.append(chunk)
                    parts.append(chunk)
                result = b"".join(parts)
                self._pending = b""
                return result
            while len(self._pending) < size:
                try:
                    chunk = next(self._iter)
                    self._buf.append(chunk)
                    self._pending += chunk
                except StopIteration:
                    break
            data, self._pending = self._pending[:size], self._pending[size:]
            return data

    class _ConcatStream:
        """Concatenate a ``BytesIO`` prefix buffer with remaining chunks from an iterator.

        This lets ijson continue parsing a response without holding the whole
        body in memory — it reads from the already-buffered prefix first, then
        fetches subsequent chunks on demand.
        """

        def __init__(self, prefix: io.BytesIO, chunks_iter: Any) -> None:
            self._prefix = prefix
            self._iter = chunks_iter
            self._pending = b""
            self._prefix_done = False

        def read(self, size: int = -1) -> bytes:
            if not self._prefix_done:
                data = self._prefix.read(size)
                if len(data) == size or size == -1:
                    # Prefix satisfied the whole read (or unlimited).
                    if size != -1 and len(data) == size:
                        return data
                # Prefix exhausted — switch to iterator.
                self._prefix_done = True
                remaining = size - len(data) if size != -1 else -1
                return data + self._read_from_iter(remaining)
            return self._read_from_iter(size)

        def _read_from_iter(self, size: int) -> bytes:
            if size == -1:
                parts = [self._pending]
                for chunk in self._iter:
                    parts.append(chunk)
                self._pending = b""
                return b"".join(parts)
            while len(self._pending) < size:
                try:
                    self._pending += next(self._iter)
                except StopIteration:
                    break
            data, self._pending = self._pending[:size], self._pending[size:]
            return data

    @staticmethod
    def _check_envelope_result(body: dict[str, Any], command: str) -> None:
        """Raise :class:`PremierClientError` if the envelope result is not OK."""
        if body.get("Result") != "OK":
            errors = body.get("Error") or []
            detail = "; ".join(f"{e.get('number')}: {e.get('desc')}" for e in errors) or "unknown error"
            raise PremierClientError(f"PREMIER command '{command}' failed: {detail}")

        for warning in body.get("Warning") or []:
            logging.warning("PREMIER warning for command '%s': %s", command, warning.get("desc"))

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

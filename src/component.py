"""PREMIER System extractor.

Row-based extractor: each configuration row pulls one business object
(customers, suppliers, issued/received invoices, products) from the PREMIER
web API and writes it to its own output table. Incremental runs use the API's
``timestamp`` change marker, persisted in the row's state file.
"""

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.vcr import DefaultSanitizer

from client import PremierClient, PremierClientError
from configuration import Configuration, ObjectType

# Scrub credentials from recorded VCR cassettes. HTTP Basic auth and the ID-UJ header
# are stripped by DefaultSanitizer's header allowlist; the extra field names cover any
# leakage into request/response bodies or URLs.
VCR_SANITIZERS = [
    DefaultSanitizer(additional_sensitive_fields=["id_uj", "ID-UJ", "#password"]),
]

# PREMIER timestamp/date formats (see API docs).
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_STATE_LAST_TIMESTAMP = "last_timestamp"
# Invoice commands (FA_OUT/FA_IN) require at least one filter; a far-past timestamp
# returns the full history while still satisfying that requirement.
_TIMESTAMP_FLOOR = "1900-01-01 00:00:00"


@dataclass(frozen=True)
class ObjectSpec:
    """Maps a configurable object to its PREMIER command, output table and behaviour.

    Attributes:
        command: PREMIER ``inComm`` command name.
        table_name: output table (without .csv).
        primary_key: columns forming the primary key (PREMIER list commands key on INTER).
        part_typ: PARTNERI filter — "OD" (customers) or "DO" (suppliers); None otherwise.
        always_timestamp: command rejects empty params, so always send a timestamp (floor on full load).
        accepts_dates: command accepts dat_od/dat_do range filters.
        needs_warehouse: command requires a warehouse number (sklad).
    """

    command: str
    table_name: str
    primary_key: list[str]
    part_typ: str | None = field(default=None)
    always_timestamp: bool = field(default=False)
    accepts_dates: bool = field(default=False)
    needs_warehouse: bool = field(default=False)


OBJECT_SPECS: dict[ObjectType, ObjectSpec] = {
    ObjectType.customers: ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD"),
    ObjectType.suppliers: ObjectSpec("PARTNERI", "suppliers", ["INTER"], part_typ="DO"),
    ObjectType.invoices_issued: ObjectSpec(
        "FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True
    ),
    ObjectType.invoices_received: ObjectSpec(
        "FA_IN", "invoices_received", ["INTER"], always_timestamp=True, accepts_dates=True
    ),
    ObjectType.products: ObjectSpec("CENIK", "products", ["INTER"], needs_warehouse=True),
}


class Component(ComponentBase):
    def __init__(self):
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)
        if self.params.debug:
            # Scope DEBUG to our own modules; keep urllib3/requests quiet so the
            # HTTP Basic Authorization header is never emitted to the job log.
            logging.getLogger("component").setLevel(logging.DEBUG)
            logging.getLogger("client").setLevel(logging.DEBUG)
            logging.getLogger("urllib3").setLevel(logging.WARNING)

    def run(self):
        """Extract one object (per the config row) and write it to an output table."""
        spec = self._resolve_object_spec()
        client = self._build_client()

        previous_state = self.get_state_file() or {}
        run_started_at = datetime.now().strftime(_TIMESTAMP_FORMAT)

        parameters = self._build_query_parameters(spec, previous_state)
        logging.info("Fetching '%s' via PREMIER command '%s'", spec.table_name, spec.command)
        try:
            records = client.execute_command(spec.command, parameters=parameters)
        except PremierClientError as exc:
            raise UserException(str(exc)) from exc
        logging.info("Received %d record(s) for '%s'", len(records), spec.table_name)

        self._save_records(records, spec)
        self.write_state_file({_STATE_LAST_TIMESTAMP: run_started_at})

    # --- setup helpers -----------------------------------------------------

    def _resolve_object_spec(self) -> ObjectSpec:
        if self.params.source.object is None:
            raise UserException("No object selected. Choose which object to extract in the row configuration.")
        return OBJECT_SPECS[self.params.source.object]

    def _build_client(self) -> PremierClient:
        conn = self.params.connection
        missing = [
            name
            for name, value in (
                ("base_url", conn.base_url),
                ("username", conn.username),
                ("#password", conn.password),
                ("id_uj", conn.id_uj),
            )
            if not value
        ]
        if missing:
            raise UserException(f"Missing required connection parameter(s): {', '.join(missing)}")
        return PremierClient(conn.base_url, conn.username, conn.password, conn.id_uj)

    def _build_query_parameters(self, spec: ObjectSpec, previous_state: dict[str, Any]) -> dict[str, Any]:
        parameters: dict[str, Any] = {}

        if spec.part_typ:
            parameters["part_typ"] = spec.part_typ

        last_timestamp = previous_state.get(_STATE_LAST_TIMESTAMP) if self.params.destination.incremental else None
        if last_timestamp:
            parameters["timestamp"] = last_timestamp
            logging.info("Incremental run: fetching records changed since %s", last_timestamp)
        elif spec.always_timestamp:
            # Full/first load of a command that requires at least one filter.
            parameters["timestamp"] = _TIMESTAMP_FLOOR

        if spec.accepts_dates:
            if self.params.source.date_from:
                parameters["dat_od"] = self.params.source.date_from
            if self.params.source.date_to:
                parameters["dat_do"] = self.params.source.date_to

        if spec.needs_warehouse:
            if self.params.source.warehouse is None:
                raise UserException("A warehouse number is required to extract products. Set 'warehouse' in the row.")
            parameters["sklad"] = self.params.source.warehouse

        return parameters

    # --- output ------------------------------------------------------------

    def _save_records(self, records: list[dict[str, Any]], spec: ObjectSpec) -> None:
        if not records:
            logging.info("No records to write for '%s' — skipping output table.", spec.table_name)
            return

        columns = self._collect_columns(records)
        primary_key = [pk for pk in spec.primary_key if pk in columns]
        if spec.primary_key and not primary_key:
            # Writing incremental=True without a primary key would append duplicates
            # every run, so fail loudly rather than silently corrupt the table.
            raise UserException(
                f"Expected primary key column(s) {spec.primary_key} are missing from the '{spec.table_name}' "
                f"response. Cannot write the table safely — the PREMIER API response shape may have changed."
            )

        table = self.create_out_table_definition(
            f"{spec.table_name}.csv",
            primary_key=primary_key,
            incremental=self.params.destination.incremental,
            columns=columns,
        )

        with open(table.full_path, mode="w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            for record in records:
                writer.writerow({col: self._serialize_value(record.get(col)) for col in columns})

        self.write_manifest(table)

    @staticmethod
    def _collect_columns(records: list[dict[str, Any]]) -> list[str]:
        """Union of keys across all records.

        Sorted to a stable order: PREMIER (an RPC API) does not guarantee key order
        between responses, and an unstable column list would read as a schema change
        to Keboola Storage on every run.
        """
        seen: set[str] = set()
        for record in records:
            seen.update(record.keys())
        return sorted(seen)

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """Flatten nested structures to JSON so they fit a single CSV cell."""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return value

    # --- sync actions ------------------------------------------------------

    @sync_action("testConnection")
    def test_connection(self) -> dict[str, str]:
        client = self._build_client()
        try:
            client.test_connection()
        except PremierClientError as exc:
            raise UserException(f"Connection failed: {exc}")
        return {"status": "success"}


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)

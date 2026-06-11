"""PREMIER System extractor.

Row-based extractor: each configuration row pulls one business object
(customers, suppliers, issued/received invoices, products) from the PREMIER
web API and writes it to its own output table. Incremental runs use the API's
``timestamp`` change marker, persisted in the row's state file.
"""

import csv
import json
import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition
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
        info_table: PREMIER table name for the INFO structure lookup (authoritative column types).
            None when the command has no directly-queryable table — types are then inferred from values.
    """

    command: str
    table_name: str
    primary_key: list[str]
    part_typ: str | None = field(default=None)
    always_timestamp: bool = field(default=False)
    accepts_dates: bool = field(default=False)
    needs_warehouse: bool = field(default=False)
    info_table: str | None = field(default=None)


OBJECT_SPECS: dict[ObjectType, ObjectSpec] = {
    # Partners — PARTNERI has no directly-queryable INFO table, so types are inferred.
    ObjectType.customers: ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD"),
    ObjectType.suppliers: ObjectSpec("PARTNERI", "suppliers", ["INTER"], part_typ="DO"),
    # Invoices and orders — require at least one filter (timestamp floor) and accept a date range.
    ObjectType.invoices_issued: ObjectSpec(
        "FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="FA_OUT"
    ),
    ObjectType.invoices_received: ObjectSpec(
        "FA_IN", "invoices_received", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="FA_IN"
    ),
    ObjectType.advance_invoices_issued: ObjectSpec(
        "FA_ZOUT", "advance_invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="FA_ZOUT"
    ),
    ObjectType.advance_invoices_received: ObjectSpec(
        "FA_ZIN", "advance_invoices_received", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="FA_ZIN"
    ),
    ObjectType.orders_received: ObjectSpec(
        "OB_IN", "orders_received", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="OB_IN"
    ),
    ObjectType.orders_issued: ObjectSpec(
        "OB_OUT", "orders_issued", ["INTER"], always_timestamp=True, accepts_dates=True, info_table="OB_OUT"
    ),
    # Stock — all require a warehouse number (sklad).
    ObjectType.products: ObjectSpec("CENIK", "products", ["INTER"], needs_warehouse=True, info_table="CENIK"),
    ObjectType.stock_receipts: ObjectSpec(
        "PRIJEMKY", "stock_receipts", ["INTER"], needs_warehouse=True, info_table="PRIJEMKY"
    ),
    ObjectType.stock_issues: ObjectSpec(
        "VYDEJKY", "stock_issues", ["INTER"], needs_warehouse=True, info_table="VYDEJKY"
    ),
    ObjectType.stock_levels: ObjectSpec(
        "SKLAD_STAV", "stock_levels", ["cislo", "sklad"], needs_warehouse=True, info_table="SKLAD_STAV"
    ),
    # Codebooks / registers — small static lists, no filters required.
    ObjectType.warehouses: ObjectSpec("SEZ_SKL", "warehouses", ["CISLO"], info_table="SEZ_SKL"),
    ObjectType.vat_rates: ObjectSpec("SAZBY_DPH", "vat_rates", ["KOD_DPH"], info_table="SAZBY_DPH"),
    ObjectType.cost_centers: ObjectSpec("STREDISK", "cost_centers", ["stkod"], info_table="STREDISK"),
    ObjectType.jobs: ObjectSpec("ZAKAZKA", "jobs", ["zkkod"], always_timestamp=True, info_table="ZAKAZKA"),
    ObjectType.document_series: ObjectSpec("DOKL_PU", "document_series", ["ID"], info_table="DOKL_PU"),
}


class Component(ComponentBase):
    def __init__(self):
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)
        if self.params.debug:
            # ComponentBase already flips the root logger to DEBUG when the debug
            # param is set, so our modules' logs surface automatically. We only need
            # to keep urllib3/requests quiet so the HTTP Basic Authorization header is
            # never emitted to the job log.
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
            record_stream = client.stream_command(spec.command, parameters=parameters)
            self._save_records(record_stream, spec, client)
        except PremierClientError as exc:
            raise UserException(str(exc)) from exc
        except (KeyError, ValueError, TypeError, OSError) as exc:
            # Type-mapping / CSV / manifest write failures are surfaced as a clean
            # user error (exit 1) instead of a generic application error (exit 2).
            raise UserException(f"Failed to write the '{spec.table_name}' output table: {exc}") from exc

        self.write_state_file({_STATE_LAST_TIMESTAMP: run_started_at})

    # --- setup helpers -----------------------------------------------------

    def _resolve_object_spec(self) -> ObjectSpec:
        if self.params.source.object is None:
            raise UserException("No object selected. Choose which object to extract in the row configuration.")
        return OBJECT_SPECS[self.params.source.object]

    def _build_client(self) -> PremierClient:
        conn = self.params.connection
        # username/password are optional — some PREMIER servers run with auth disabled.
        missing = [name for name, value in (("base_url", conn.base_url), ("id_uj", conn.id_uj)) if not value]
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

    # Maximum number of records held in the value-sample buffer for type
    # inference on columns not covered by INFO. After this many records the
    # sample is frozen; additional records are streamed straight to CSV.
    _TYPE_SAMPLE_SIZE = 200

    def _save_records(self, record_stream: Iterable[dict[str, Any]], spec: ObjectSpec, client: PremierClient) -> None:
        """Stream records from *record_stream* to the output CSV table.

        The approach is a single pass with a bounded look-aside sample:

        1. The first record fixes the column set (PREMIER responses are tabular
           so all records share the same schema).
        2. Up to ``_TYPE_SAMPLE_SIZE`` records are buffered in memory to allow
           value-based type inference for columns not described by INFO.
        3. All records are written to a temporary file first (no header), then
           the column set and type information are known and the final output
           CSV is written with a header followed by all rows from the temp file.

        INFO types are fetched up-front (before streaming begins) via
        :meth:`_fetch_table_types` and take precedence over value inference.
        This means streaming and authoritative type information are fully
        compatible.
        """
        stream_iter: Iterator[dict[str, Any]] = iter(record_stream)

        # Peek at the first record to detect empty result and discover columns.
        # INFO is fetched AFTER the first data record is confirmed — this
        # preserves the original HTTP call order (data command first, then INFO)
        # which is important for VCR cassette replay.
        try:
            first_record = next(stream_iter)
        except StopIteration:
            logging.info("No records to write for '%s' — skipping output table.", spec.table_name)
            return

        # Derive columns from the first record.  PREMIER is a SQL Server–backed
        # RPC API; all records in a single response share the same column set.
        # We use a sorted union in case later records add unexpected keys.
        columns: list[str] = sorted(first_record.keys())
        column_set: set[str] = set(columns)

        primary_key = [pk for pk in spec.primary_key if pk in column_set]
        if spec.primary_key and not primary_key:
            raise UserException(
                f"Expected primary key column(s) {spec.primary_key} are missing from the '{spec.table_name}' "
                f"response. Cannot write the table safely — the PREMIER API response shape may have changed."
            )

        # Fetch authoritative column types from INFO now — after the data
        # command has already been sent.  This preserves the historical
        # HTTP call order (data first, INFO second) that the VCR cassettes
        # were recorded with.
        type_fields = self._fetch_table_types(client, spec.info_table)

        # --- Stream all records to a temp file while collecting samples ----
        # value_samples[col] = list of non-null values seen so far (capped at _TYPE_SAMPLE_SIZE)
        value_samples: dict[str, list[Any]] = {col: [] for col in columns}
        records_written = 0

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", newline="") as tmp:
            # Write rows without a header; columns may grow as new records are seen.
            # We use a plain csv.writer and serialise each field in column order.
            csv_writer = csv.writer(tmp)

            for record in [first_record, *stream_iter]:
                # Discover any new columns introduced by this record.
                new_keys = record.keys() - column_set
                if new_keys:
                    columns = sorted(column_set | new_keys)
                    column_set = set(columns)
                    for k in new_keys:
                        value_samples[k] = []

                csv_writer.writerow([self._serialize_value(record.get(col)) for col in columns])
                records_written += 1

                if records_written <= self._TYPE_SAMPLE_SIZE:
                    for col in columns:
                        v = record.get(col)
                        if v is not None:
                            value_samples[col].append(v)

            logging.info("Received %d record(s) for '%s'", records_written, spec.table_name)

            # --- Build schema from INFO + value samples --------------------
            schema = self._build_schema(value_samples, columns, primary_key, type_fields)
            table = self.create_out_table_definition(
                f"{spec.table_name}.csv",
                primary_key=primary_key,
                incremental=self.params.destination.incremental,
                schema=schema,
                has_header=True,
            )

            # --- Write final output file: header + rows from temp ----------
            tmp.seek(0)
            with open(table.full_path, mode="w", encoding="utf-8", newline="") as out_file:
                out_writer = csv.writer(out_file)
                out_writer.writerow(columns)  # header
                for row in csv.reader(tmp):
                    out_writer.writerow(row)

        self.write_manifest(table)

    @staticmethod
    def _fetch_table_types(client: PremierClient, info_table: str | None) -> dict[str, dict[str, Any]]:
        """Fetch authoritative column metadata for a table via the INFO command.

        Returns a map keyed by UPPERCASED column name (PREMIER responses are not
        case-consistent). Returns an empty map — and never raises — when no table is
        configured or INFO fails, so the caller falls back to value-based inference.
        """
        if not info_table:
            return {}
        try:
            data = client.execute_command("INFO", parameters={"table_name": info_table})
        except PremierClientError as exc:
            logging.warning("Could not load column types from INFO for table '%s': %s", info_table, exc)
            return {}

        fields: dict[str, dict[str, Any]] = {}
        if isinstance(data, list) and data and isinstance(data[0], dict):
            for field_def in data[0].get("tableStruct", {}).get("tabFields", []):
                name = field_def.get("fiels_name")  # note: PREMIER's key is "fiels_name"
                if name:
                    fields[name.upper()] = field_def
        return fields

    @staticmethod
    def _build_schema(
        value_samples: dict[str, list[Any]],
        columns: list[str],
        primary_key: list[str],
        type_fields: dict[str, dict[str, Any]],
    ) -> dict[str, ColumnDefinition]:
        """Build a typed manifest schema.

        Uses PREMIER's authoritative column metadata (INFO) where available, and falls
        back to inferring the type from a bounded value sample for columns INFO doesn't
        describe.

        Args:
            value_samples: mapping of column name → list of non-null sample values.
                           Built during streaming; capped at ``_TYPE_SAMPLE_SIZE``.
            columns: ordered list of all column names.
            primary_key: column names that form the primary key.
            type_fields: INFO metadata keyed by UPPERCASED column name.
        """
        pk = set(primary_key)
        schema: dict[str, ColumnDefinition] = {}
        for column in columns:
            is_pk = column in pk
            meta = type_fields.get(column.upper())
            if meta:
                base_type = Component._premier_type_to_base(meta)
            else:
                base_type = Component._infer_base_type(value_samples.get(column) or [])
            # Non-PK columns are left nullable: a command's joined/computed response can
            # carry nulls even where the base table column is NOT NULL, and a strict
            # non-nullable manifest would then fail the Storage load.
            schema[column] = ColumnDefinition(data_types=base_type, primary_key=is_pk, nullable=not is_pk)
        return schema

    @staticmethod
    def _premier_type_to_base(meta: dict[str, Any]) -> BaseType:
        """Map a PREMIER (SQL Server) column definition to a Keboola base type."""
        sql_type = str(meta.get("field_type") or "").lower()
        width = meta.get("field_width")
        decimals = meta.get("field_decimal") or 0

        if sql_type == "bit":
            return BaseType.boolean()
        if sql_type == "date":
            return BaseType.date()
        if sql_type in ("datetime", "datetime2", "smalldatetime"):
            return BaseType.timestamp()
        if sql_type in ("decimal", "numeric", "money", "smallmoney"):
            if decimals and int(decimals) > 0:
                # Without a precision (width) the length spec would be "None,2"; emit a
                # length-less NUMERIC in that case so Storage applies its default precision.
                if width:
                    return BaseType.numeric(length=f"{width},{decimals}")
                return BaseType.numeric()
            return BaseType.integer()
        if sql_type in ("int", "smallint", "tinyint", "bigint"):
            return BaseType.integer()
        if sql_type in ("float", "real"):
            return BaseType.float()
        if sql_type in ("char", "varchar", "nchar", "nvarchar") and width:
            return BaseType.string(length=str(width))
        # text/ntext/memo, uniqueidentifier, timestamp (SQL rowversion), binary, unknown → STRING
        return BaseType.string()

    @staticmethod
    def _infer_base_type(values: list[Any]) -> BaseType:
        """Map the Python types of a column's non-null values to a Keboola base type."""
        if not values:
            return BaseType.string()
        if all(isinstance(v, bool) for v in values):
            return BaseType.boolean()
        # bool is a subclass of int, so exclude it from the numeric checks.
        non_bool = [v for v in values if not isinstance(v, bool)]
        if len(non_bool) == len(values):
            if all(isinstance(v, int) for v in non_bool):
                return BaseType.integer()
            if all(isinstance(v, (int, float)) for v in non_bool):
                return BaseType.numeric()
        return BaseType.string()

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
        """Render a value for a single CSV cell.

        Booleans become lowercase ``true``/``false`` so the BOOLEAN base type parses
        them; nested structures are JSON-encoded; scalars pass through unchanged.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
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
            raise UserException(f"Connection failed: {exc}") from exc
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

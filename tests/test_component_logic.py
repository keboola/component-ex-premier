"""Unit tests for Component helper methods.

These tests exercise individual methods of the Component class without making
any HTTP calls and without going through the full run() pipeline. Each test
points KBC_DATADIR at a minimal fixture directory that provides a valid
config.json so Component.__init__ succeeds.
"""

import csv
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from keboola.component.exceptions import UserException

# Ensure src is importable (already done by tests/__init__.py, but explicit here too)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keboola.component.dao import SupportedDataTypes

from client import PremierClientError
from component import _TIMESTAMP_FLOOR, OBJECT_SPECS, Component, ObjectSpec
from configuration import ObjectType

# ---------------------------------------------------------------------------
# Fixture datadir that has a complete, valid config (no live API needed)
# ---------------------------------------------------------------------------
_UNIT_HELPER_DATADIR = str(Path(__file__).resolve().parent / "data" / "test_unit_helper")


def _make_component() -> Component:
    """Instantiate Component pointing at the unit-helper fixture datadir."""
    os.environ["KBC_DATADIR"] = _UNIT_HELPER_DATADIR
    return Component()


# ---------------------------------------------------------------------------
# _build_query_parameters
# ---------------------------------------------------------------------------


class TestBuildQueryParameters(unittest.TestCase):
    def setUp(self):
        self.comp = _make_component()

    # --- customers spec (part_typ="OD", no always_timestamp) ---

    def test_part_typ_od_added_for_customers(self):
        """Customers spec must add part_typ=OD."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "customers"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD")
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["part_typ"], "OD")

    def test_part_typ_do_added_for_suppliers(self):
        """Suppliers spec must add part_typ=DO."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "suppliers"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("PARTNERI", "suppliers", ["INTER"], part_typ="DO")
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["part_typ"], "DO")

    def test_no_timestamp_for_customers_full_load_no_state(self):
        """Customers (no always_timestamp) full load with no state: no timestamp."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "customers"},
            destination={"load_type": "full_load"},
        )
        spec = ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD")
        params = self.comp._build_query_parameters(spec, {})
        self.assertNotIn("timestamp", params)

    def test_no_timestamp_when_non_incremental_even_with_state(self):
        """Full load should never add a timestamp, even if state exists."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "customers"},
            destination={"load_type": "full_load"},
        )
        spec = ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD")
        state = {"last_timestamp": "2024-01-01 00:00:00"}
        params = self.comp._build_query_parameters(spec, state)
        self.assertNotIn("timestamp", params)

    # --- invoices spec (always_timestamp=True, accepts_dates=True) ---

    def test_timestamp_floor_on_full_load_when_always_timestamp(self):
        """FA_OUT/FA_IN full load with no state: must add timestamp=_TIMESTAMP_FLOOR."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued"},
            destination={"load_type": "full_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["timestamp"], _TIMESTAMP_FLOOR)

    def test_timestamp_floor_when_incremental_but_no_state(self):
        """Incremental first run (no state) + always_timestamp: use floor."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["timestamp"], _TIMESTAMP_FLOOR)

    def test_timestamp_added_when_incremental_and_state_present(self):
        """Incremental run with state: must use state's last_timestamp."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        state = {"last_timestamp": "2024-06-01 08:00:00"}
        params = self.comp._build_query_parameters(spec, state)
        self.assertEqual(params["timestamp"], "2024-06-01 08:00:00")

    def test_no_timestamp_when_incremental_but_state_has_no_timestamp_key(self):
        """State dict exists but missing last_timestamp key: fall through to always_timestamp."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "customers"},
            destination={"load_type": "incremental_load"},
        )
        # customers has no always_timestamp, so no timestamp at all
        spec = ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD")
        params = self.comp._build_query_parameters(spec, {"some_other_key": "value"})
        self.assertNotIn("timestamp", params)

    # --- dates ---

    def test_dat_od_added_when_date_from_set(self):
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued", "date_from": "2024-01-01", "date_to": ""},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["dat_od"], "2024-01-01")

    def test_dat_do_added_when_date_to_set(self):
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued", "date_from": "", "date_to": "2024-12-31"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["dat_do"], "2024-12-31")

    def test_empty_date_from_is_not_included(self):
        """No date_from in source → no dat_od in params."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued", "date_from": "", "date_to": ""},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertNotIn("dat_od", params)

    def test_dates_not_included_when_spec_does_not_accept_dates(self):
        """Customers spec (accepts_dates=False) must NOT emit dat_od/dat_do."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "customers", "date_from": "2024-01-01", "date_to": "2024-12-31"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("PARTNERI", "customers", ["INTER"], part_typ="OD")
        params = self.comp._build_query_parameters(spec, {})
        self.assertNotIn("dat_od", params)
        self.assertNotIn("dat_do", params)

    def test_all_three_params_together(self):
        """Incremental invoices with dates: timestamp + dat_od + dat_do all present."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued", "date_from": "2024-01-01", "date_to": "2024-12-31"},
            destination={"load_type": "incremental_load"},
        )
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"], always_timestamp=True, accepts_dates=True)
        state = {"last_timestamp": "2024-01-01 00:00:00"}
        params = self.comp._build_query_parameters(spec, state)
        self.assertEqual(params["timestamp"], "2024-01-01 00:00:00")
        self.assertEqual(params["dat_od"], "2024-01-01")
        self.assertEqual(params["dat_do"], "2024-12-31")

    # --- products (needs_warehouse) ---

    def test_sklad_added_for_products_when_warehouse_set(self):
        """Products spec: sklad must equal source.warehouse."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "products", "warehouse": 5},
            destination={"load_type": "full_load"},
        )
        spec = ObjectSpec("CENIK", "products", ["INTER"], needs_warehouse=True)
        params = self.comp._build_query_parameters(spec, {})
        self.assertEqual(params["sklad"], 5)

    def test_raises_user_exception_when_warehouse_missing_for_products(self):
        """Products spec with warehouse=None must raise UserException."""
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "products", "warehouse": None},
            destination={"load_type": "full_load"},
        )
        spec = ObjectSpec("CENIK", "products", ["INTER"], needs_warehouse=True)
        with self.assertRaises(UserException) as ctx:
            self.comp._build_query_parameters(spec, {})
        self.assertIn("warehouse", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# _serialize_value
# ---------------------------------------------------------------------------


class TestSerializeValue(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(Component._serialize_value("hello"), "hello")

    def test_int_passthrough(self):
        self.assertEqual(Component._serialize_value(42), 42)

    def test_float_passthrough(self):
        self.assertEqual(Component._serialize_value(3.14), 3.14)

    def test_none_passthrough(self):
        self.assertIsNone(Component._serialize_value(None))

    def test_bool_true_renders_as_lowercase_string(self):
        self.assertEqual(Component._serialize_value(True), "true")

    def test_bool_false_renders_as_lowercase_string(self):
        self.assertEqual(Component._serialize_value(False), "false")

    def test_dict_is_json_encoded(self):
        val = {"key": "value", "num": 1}
        result = Component._serialize_value(val)
        self.assertIsInstance(result, str)
        self.assertEqual(json.loads(result), val)

    def test_list_is_json_encoded(self):
        val = [1, 2, {"nested": True}]
        result = Component._serialize_value(val)
        self.assertIsInstance(result, str)
        self.assertEqual(json.loads(result), val)

    def test_nested_dict_in_list_is_json_encoded(self):
        val = [{"a": 1}, {"b": 2}]
        result = Component._serialize_value(val)
        self.assertEqual(json.loads(result), val)

    def test_unicode_preserved_in_dict(self):
        val = {"name": "Novák"}
        result = Component._serialize_value(val)
        self.assertIn("Novák", result)


# ---------------------------------------------------------------------------
# _infer_base_type
# ---------------------------------------------------------------------------


class TestInferBaseType(unittest.TestCase):
    def _dtype(self, values):
        """Return the SupportedDataTypes for inferred base type."""
        return Component._infer_base_type(values)["base"].dtype

    def test_empty_list_returns_string(self):
        self.assertEqual(self._dtype([]), SupportedDataTypes.STRING)

    def test_all_bool_returns_boolean(self):
        self.assertEqual(self._dtype([True, False, True]), SupportedDataTypes.BOOLEAN)

    def test_all_int_returns_integer(self):
        self.assertEqual(self._dtype([1, 2, 3]), SupportedDataTypes.INTEGER)

    def test_all_float_returns_numeric(self):
        self.assertEqual(self._dtype([1.5, 2.0, 3.7]), SupportedDataTypes.NUMERIC)

    def test_mixed_int_and_float_returns_numeric(self):
        self.assertEqual(self._dtype([1, 2.5, 3]), SupportedDataTypes.NUMERIC)

    def test_all_string_returns_string(self):
        self.assertEqual(self._dtype(["a", "b"]), SupportedDataTypes.STRING)

    def test_mixed_int_and_string_returns_string(self):
        self.assertEqual(self._dtype([1, "two"]), SupportedDataTypes.STRING)

    def test_bool_not_treated_as_integer(self):
        """bool is a subclass of int; a list of bools must be BOOLEAN not INTEGER."""
        self.assertNotEqual(self._dtype([True, False]), SupportedDataTypes.INTEGER)
        self.assertEqual(self._dtype([True, False]), SupportedDataTypes.BOOLEAN)

    def test_mixed_bool_and_int_returns_string(self):
        """Mixed booleans and integers (not all bool) must fall back to STRING."""
        self.assertEqual(self._dtype([True, 1, 2]), SupportedDataTypes.STRING)

    def test_single_int_returns_integer(self):
        self.assertEqual(self._dtype([42]), SupportedDataTypes.INTEGER)

    def test_single_float_returns_numeric(self):
        self.assertEqual(self._dtype([3.14]), SupportedDataTypes.NUMERIC)


# ---------------------------------------------------------------------------
# _build_schema
# ---------------------------------------------------------------------------


class TestBuildSchema(unittest.TestCase):
    """_build_schema takes value_samples (dict[col, list[non-null values]]) in the streaming API."""

    def test_pk_column_has_primary_key_true_and_nullable_false(self):
        samples = {"AMOUNT": [500], "INTER": ["FA-001"]}
        columns = ["AMOUNT", "INTER"]
        schema = Component._build_schema(samples, columns, primary_key=["INTER"], type_fields={})
        self.assertTrue(schema["INTER"].primary_key)
        self.assertFalse(schema["INTER"].nullable)

    def test_non_pk_column_is_nullable(self):
        samples = {"AMOUNT": [500], "INTER": ["FA-001"]}
        columns = ["AMOUNT", "INTER"]
        schema = Component._build_schema(samples, columns, primary_key=["INTER"], type_fields={})
        self.assertTrue(schema["AMOUNT"].nullable)
        self.assertFalse(schema["AMOUNT"].primary_key)

    def test_integer_column_inferred_correctly(self):
        samples = {"ID": [1, 2]}
        schema = Component._build_schema(samples, ["ID"], primary_key=[], type_fields={})
        self.assertEqual(schema["ID"].data_types["base"].dtype, SupportedDataTypes.INTEGER)

    def test_boolean_column_inferred_correctly(self):
        samples = {"FLAG": [True, False]}
        schema = Component._build_schema(samples, ["FLAG"], primary_key=[], type_fields={})
        self.assertEqual(schema["FLAG"].data_types["base"].dtype, SupportedDataTypes.BOOLEAN)

    def test_string_column_inferred_correctly(self):
        samples = {"NAME": ["Alice", "Bob"]}
        schema = Component._build_schema(samples, ["NAME"], primary_key=[], type_fields={})
        self.assertEqual(schema["NAME"].data_types["base"].dtype, SupportedDataTypes.STRING)

    def test_all_columns_present_in_schema(self):
        samples = {"A": [1, 2], "B": ["x", "y"]}
        schema = Component._build_schema(samples, ["A", "B"], primary_key=["A"], type_fields={})
        self.assertIn("A", schema)
        self.assertIn("B", schema)

    def test_type_fields_overrides_value_inference(self):
        """A column whose values are all strings but INFO says decimal/2 must be NUMERIC."""
        samples = {"CELKEM": ["1234.56", "789.00"]}
        type_fields = {
            "CELKEM": {
                "fiels_name": "CELKEM",
                "field_type": "decimal",
                "field_width": 17,
                "field_decimal": 2,
                "fields_null": True,
            },
        }
        schema = Component._build_schema(samples, ["CELKEM"], primary_key=[], type_fields=type_fields)
        self.assertEqual(schema["CELKEM"].data_types["base"].dtype, SupportedDataTypes.NUMERIC)


# ---------------------------------------------------------------------------
# _save_records
# ---------------------------------------------------------------------------


class TestSaveRecords(unittest.TestCase):
    def setUp(self):
        self.comp = _make_component()
        # A mock client that returns empty type_fields (no INFO table configured on spec)
        self.mock_client = MagicMock()
        self.mock_client.execute_command.return_value = []

    def test_skips_output_when_records_empty(self):
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        with patch.object(self.comp, "create_out_table_definition") as mock_create:
            self.comp._save_records([], spec, self.mock_client)
        mock_create.assert_not_called()

    def test_raises_user_exception_when_pk_columns_entirely_absent(self):
        """If every PK column is absent from all records, _save_records must raise UserException."""
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"AMOUNT": 100, "DATE": "2024-01-01"}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(UserException) as ctx:
            self.comp._save_records(records, spec, self.mock_client)

        self.assertIn("INTER", str(ctx.exception))
        # No output file must have been written.
        self.assertFalse((out_dir / "invoices_issued.csv").exists())

    def test_primary_key_present_when_column_exists(self):
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"INTER": "FA-001", "AMOUNT": 500}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        self.comp._save_records(records, spec, self.mock_client)

        with open(out_dir / "invoices_issued.csv.manifest") as f:
            manifest = json.load(f)
        # The keboola SDK uses a schema-based manifest: PK is indicated per column.
        pk_columns = [col["name"] for col in manifest.get("schema", []) if col.get("primary_key")]
        self.assertIn("INTER", pk_columns)

    def test_csv_row_written_correctly(self):
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"INTER": "FA-001", "AMOUNT": 1000}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        self.comp._save_records(records, spec, self.mock_client)

        # The component now writes a header row followed by data rows.
        # Columns are sorted alphabetically by _collect_columns: AMOUNT < INTER.
        with open(out_dir / "invoices_issued.csv") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ["AMOUNT", "INTER"])  # header row
        self.assertEqual(rows[1][0], "1000")  # AMOUNT column (sorted first)
        self.assertEqual(rows[1][1], "FA-001")  # INTER column (sorted second)

    def test_csv_has_header_has_header_in_manifest(self):
        """The manifest must carry has_header=True when using the typed schema."""
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"INTER": "FA-001", "AMOUNT": 1000}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        self.comp._save_records(records, spec, self.mock_client)

        with open(out_dir / "invoices_issued.csv.manifest") as f:
            manifest = json.load(f)
        self.assertTrue(manifest.get("has_header"))

    def test_generator_not_drained_into_list(self):
        """_save_records must iterate the stream in a single pass (finding 1).

        The old pattern ``for record in [first_record, *stream_iter]`` unpacks
        the entire generator into a list before the loop body runs for record 0.
        This holds ALL records in memory at once, defeating streaming.

        Detection strategy: wrap the record stream in a generator that raises
        ``RuntimeError`` when item 2 is requested AND an out-of-band flag says
        that the temp-file write of item 0 has NOT happened yet.

        - Drain path  : list() pulls items 0, 1, 2 before the for-loop starts.
          Item 2 is requested while the flag is still False → RuntimeError.
        - Single-pass : items are consumed one at a time inside the for-loop.
          Item 0's writerow runs and sets the flag to True BEFORE item 2 is
          requested → no error.

        We set the flag by patching ``component.tempfile.TemporaryFile`` to
        intercept the write path.  However, patching C-extension csv.writer is
        simpler via wrapping the TemporaryFile object.

        Simpler alternative: patch ``component.csv`` so that the first call to
        csv.writer returns a wrapper that sets a flag on first ``writerow``.
        """
        import csv as csv_module

        import component as comp_module

        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        first_write_done = [False]
        items_yielded_count = [0]

        class WriterWrapper:
            """Thin wrapper around csv.writer that sets first_write_done on first writerow."""

            def __init__(self, f):
                self._w = csv_module.writer(f)

            def writerow(self, row):
                first_write_done[0] = True
                self._w.writerow(row)

        def lazy_records():
            for i in range(3):
                items_yielded_count[0] += 1
                if i == 2 and not first_write_done[0]:
                    raise AssertionError(
                        "Item 2 was consumed before the first writerow — generator was drained into a list."
                    )
                yield {"INTER": f"FA-{i:03d}", "AMOUNT": i * 100}

        with patch.object(comp_module, "csv") as mock_csv_mod:
            # Delegate everything to the real csv module; only override writer.
            mock_csv_mod.writer = WriterWrapper
            mock_csv_mod.reader = csv_module.reader
            self.comp._save_records(lazy_records(), spec, self.mock_client)

        # Output correctness: header + 3 data rows
        out_path = Path(_UNIT_HELPER_DATADIR) / "out" / "tables" / "invoices_issued.csv"
        with open(out_path) as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 4)  # header + 3 data rows

    def tearDown(self):
        """Clean written output files so tests don't interfere with each other."""
        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        if out_dir.exists():
            for f in out_dir.glob("*"):
                if f.is_file():
                    f.unlink()


# ---------------------------------------------------------------------------
# _build_schema with streaming (column discovery via sample)
# ---------------------------------------------------------------------------


class TestBuildSchemaStreaming(unittest.TestCase):
    """_build_schema must work when type_fields cover all columns (INFO path).

    Under streaming, the component fetches INFO types up-front (before writing
    records) and uses those for all schema columns.  Value-based inference is
    used only for columns that INFO did not describe — in that case the component
    collects a bounded sample during streaming and infers from that sample.
    """

    def test_info_types_take_priority_over_value_inference(self):
        """If INFO covers a column, its type must be used regardless of the value type."""
        samples = {"CELKEM": ["1234.56", "789.00"]}
        type_fields = {
            "CELKEM": {
                "fiels_name": "CELKEM",
                "field_type": "decimal",
                "field_width": 17,
                "field_decimal": 2,
                "fields_null": True,
            }
        }
        schema = Component._build_schema(samples, ["CELKEM"], primary_key=[], type_fields=type_fields)
        from keboola.component.dao import SupportedDataTypes

        self.assertEqual(schema["CELKEM"].data_types["base"].dtype, SupportedDataTypes.NUMERIC)

    def test_value_inference_used_for_columns_not_in_info(self):
        """Columns absent from INFO must still get their type inferred from values."""
        samples = {"AMOUNT": [100, 200]}
        schema = Component._build_schema(samples, ["AMOUNT"], primary_key=[], type_fields={})
        from keboola.component.dao import SupportedDataTypes

        self.assertEqual(schema["AMOUNT"].data_types["base"].dtype, SupportedDataTypes.INTEGER)

    def test_schema_built_from_empty_sample_defaults_to_string(self):
        """A column with no non-null values in the sample must default to STRING."""
        samples = {"X": []}  # no non-null values collected
        schema = Component._build_schema(samples, ["X"], primary_key=[], type_fields={})
        from keboola.component.dao import SupportedDataTypes

        self.assertEqual(schema["X"].data_types["base"].dtype, SupportedDataTypes.STRING)


# ---------------------------------------------------------------------------
# _resolve_object_spec / _build_client — error cases
# ---------------------------------------------------------------------------


class TestResolveObjectSpec(unittest.TestCase):
    def test_raises_user_exception_when_no_object_selected(self):
        from configuration import Configuration

        comp = _make_component()
        comp.params = Configuration(
            connection=comp.params.connection.model_dump(by_alias=True),
            source={"object": None, "date_from": "", "date_to": ""},
            destination={"load_type": "incremental_load"},
        )
        with self.assertRaises(UserException) as ctx:
            comp._resolve_object_spec()
        self.assertIn("No object selected", str(ctx.exception))


class TestBuildClient(unittest.TestCase):
    def _comp_with_connection(self, **overrides):
        from configuration import Configuration

        comp = _make_component()
        base = {
            "base_url": "https://premier.example.com",
            "username": "testuser",
            "#password": "testpass",
            "id_uj": "aaaa-bbbb",
        }
        base.update(overrides)
        comp.params = Configuration(
            connection=base,
            source={"object": "invoices_issued", "date_from": "", "date_to": ""},
            destination={"load_type": "incremental_load"},
        )
        return comp

    def test_raises_when_base_url_missing(self):
        comp = self._comp_with_connection(**{"base_url": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        self.assertIn("base_url", str(ctx.exception))

    def test_raises_when_id_uj_missing(self):
        comp = self._comp_with_connection(**{"id_uj": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        self.assertIn("id_uj", str(ctx.exception))

    def test_raises_lists_all_missing_required_fields(self):
        """Only base_url and id_uj are required; username/password are optional."""
        comp = self._comp_with_connection(**{"base_url": "", "id_uj": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        msg = str(ctx.exception)
        self.assertIn("base_url", msg)
        self.assertIn("id_uj", msg)

    def test_returns_client_when_all_fields_present(self):
        from client import PremierClient

        comp = self._comp_with_connection()
        client = comp._build_client()
        self.assertIsInstance(client, PremierClient)

    def test_returns_anonymous_client_when_username_and_password_omitted(self):
        """username/password are optional — omitting them must NOT raise."""
        from client import PremierClient

        comp = self._comp_with_connection(**{"username": "", "#password": ""})
        client = comp._build_client()
        self.assertIsInstance(client, PremierClient)

    def test_returns_client_when_only_password_omitted(self):
        """Omitting only the password (with a username) must NOT raise — auth is optional."""
        from client import PremierClient

        comp = self._comp_with_connection(**{"#password": ""})
        client = comp._build_client()
        self.assertIsInstance(client, PremierClient)


# ---------------------------------------------------------------------------
# run() — no object selected
# ---------------------------------------------------------------------------


class TestRunErrors(unittest.TestCase):
    def test_run_raises_user_exception_when_object_is_none(self):
        from configuration import Configuration

        comp = _make_component()
        comp.params = Configuration(
            connection=comp.params.connection.model_dump(by_alias=True),
            source={"object": None, "date_from": "", "date_to": ""},
            destination={"load_type": "incremental_load"},
        )
        with self.assertRaises(UserException):
            comp.run()


# ---------------------------------------------------------------------------
# run() — streaming integration
# ---------------------------------------------------------------------------


class TestRunStreaming(unittest.TestCase):
    """run() must use stream_command (not execute_command) for data extraction."""

    def setUp(self):
        self.comp = _make_component()
        from configuration import Configuration

        self.comp.params = Configuration(
            connection=self.comp.params.connection.model_dump(by_alias=True),
            source={"object": "invoices_issued"},
            destination={"load_type": "full_load"},
        )
        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        if out_dir.exists():
            for f in out_dir.glob("*"):
                if f.is_file():
                    f.unlink()

    def test_run_uses_stream_command_not_execute_command(self):
        """run() must call client.stream_command to extract data records."""
        from client import PremierClient

        mock_client = MagicMock(spec=PremierClient)
        # stream_command returns a generator; simulate an empty result
        mock_client.stream_command.return_value = iter([])
        mock_client.execute_command.return_value = []  # for INFO call

        with patch.object(self.comp, "_build_client", return_value=mock_client):
            self.comp.run()

        mock_client.stream_command.assert_called_once()
        call_args = mock_client.stream_command.call_args
        self.assertEqual(call_args[0][0], "FA_OUT")

    def test_run_writes_csv_from_streamed_records(self):
        """run() must produce a CSV from records yielded by stream_command."""
        from client import PremierClient

        records = [{"INTER": 1, "AMOUNT": 500}, {"INTER": 2, "AMOUNT": 750}]
        mock_client = MagicMock(spec=PremierClient)
        mock_client.stream_command.return_value = iter(records)
        mock_client.execute_command.return_value = []  # INFO returns nothing

        with patch.object(self.comp, "_build_client", return_value=mock_client):
            self.comp.run()

        out_path = Path(_UNIT_HELPER_DATADIR) / "out" / "tables" / "invoices_issued.csv"
        self.assertTrue(out_path.exists())
        with open(out_path) as f:
            rows = list(csv.reader(f))
        # Header + 2 data rows
        self.assertEqual(len(rows), 3)
        self.assertIn("INTER", rows[0])
        self.assertIn("AMOUNT", rows[0])


# ---------------------------------------------------------------------------
# _premier_type_to_base
# ---------------------------------------------------------------------------


class TestPremierTypeToBase(unittest.TestCase):
    def _dtype(self, meta):
        """Return the SupportedDataTypes for a given field meta dict."""
        return Component._premier_type_to_base(meta)["base"].dtype

    def _bt(self, meta):
        """Return the full BaseType dict for a given field meta dict."""
        return Component._premier_type_to_base(meta)

    def test_bit_returns_boolean(self):
        self.assertEqual(
            self._dtype({"field_type": "bit", "field_width": 1, "field_decimal": 0}), SupportedDataTypes.BOOLEAN
        )

    def test_decimal_with_decimals_returns_numeric(self):
        self.assertEqual(
            self._dtype({"field_type": "decimal", "field_width": 17, "field_decimal": 2}),
            SupportedDataTypes.NUMERIC,
        )

    def test_decimal_with_decimals_has_correct_length(self):
        """NUMERIC for decimal with decimals must encode length as 'width,dec'."""
        bt = self._bt({"field_type": "decimal", "field_width": 17, "field_decimal": 2})
        self.assertEqual(bt["base"].length, "17,2")

    def test_decimal_with_decimals_but_no_width_returns_lengthless_numeric(self):
        """A decimal column whose INFO width is None must yield NUMERIC with no length,
        not a broken 'None,2' length spec (which would raise on the output path)."""
        bt = self._bt({"field_type": "decimal", "field_width": None, "field_decimal": 2})
        self.assertEqual(bt["base"].dtype, SupportedDataTypes.NUMERIC)
        self.assertIsNone(bt["base"].length)

    def test_decimal_zero_decimals_returns_integer(self):
        self.assertEqual(
            self._dtype({"field_type": "decimal", "field_width": 10, "field_decimal": 0}),
            SupportedDataTypes.INTEGER,
        )

    def test_datetime_returns_timestamp(self):
        self.assertEqual(
            self._dtype({"field_type": "datetime", "field_width": None, "field_decimal": 0}),
            SupportedDataTypes.TIMESTAMP,
        )

    def test_datetime2_returns_timestamp(self):
        self.assertEqual(
            self._dtype({"field_type": "datetime2", "field_width": None, "field_decimal": 0}),
            SupportedDataTypes.TIMESTAMP,
        )

    def test_smalldatetime_returns_timestamp(self):
        self.assertEqual(
            self._dtype({"field_type": "smalldatetime", "field_width": None, "field_decimal": 0}),
            SupportedDataTypes.TIMESTAMP,
        )

    def test_date_returns_date(self):
        self.assertEqual(
            self._dtype({"field_type": "date", "field_width": None, "field_decimal": 0}),
            SupportedDataTypes.DATE,
        )

    def test_int_returns_integer(self):
        self.assertEqual(
            self._dtype({"field_type": "int", "field_width": 4, "field_decimal": 0}),
            SupportedDataTypes.INTEGER,
        )

    def test_smallint_returns_integer(self):
        self.assertEqual(
            self._dtype({"field_type": "smallint", "field_width": 2, "field_decimal": 0}),
            SupportedDataTypes.INTEGER,
        )

    def test_bigint_returns_integer(self):
        self.assertEqual(
            self._dtype({"field_type": "bigint", "field_width": 8, "field_decimal": 0}),
            SupportedDataTypes.INTEGER,
        )

    def test_float_returns_float(self):
        self.assertEqual(
            self._dtype({"field_type": "float", "field_width": 8, "field_decimal": 0}),
            SupportedDataTypes.FLOAT,
        )

    def test_real_returns_float(self):
        self.assertEqual(
            self._dtype({"field_type": "real", "field_width": 4, "field_decimal": 0}),
            SupportedDataTypes.FLOAT,
        )

    def test_char_with_width_returns_string_with_length(self):
        bt = self._bt({"field_type": "char", "field_width": 20, "field_decimal": 0})
        self.assertEqual(bt["base"].dtype, SupportedDataTypes.STRING)
        self.assertEqual(bt["base"].length, "20")

    def test_varchar_with_width_returns_string_with_length(self):
        bt = self._bt({"field_type": "varchar", "field_width": 255, "field_decimal": 0})
        self.assertEqual(bt["base"].dtype, SupportedDataTypes.STRING)
        self.assertEqual(bt["base"].length, "255")

    def test_uniqueidentifier_returns_string(self):
        self.assertEqual(
            self._dtype({"field_type": "uniqueidentifier", "field_width": 16, "field_decimal": 0}),
            SupportedDataTypes.STRING,
        )

    def test_timestamp_rowversion_returns_string(self):
        """SQL timestamp (rowversion) is NOT a datetime — must map to STRING."""
        self.assertEqual(
            self._dtype({"field_type": "timestamp", "field_width": 8, "field_decimal": 0}),
            SupportedDataTypes.STRING,
        )

    def test_unknown_type_returns_string(self):
        self.assertEqual(
            self._dtype({"field_type": "binary", "field_width": 8, "field_decimal": 0}),
            SupportedDataTypes.STRING,
        )

    def test_empty_type_returns_string(self):
        self.assertEqual(
            self._dtype({"field_type": "", "field_width": None, "field_decimal": 0}),
            SupportedDataTypes.STRING,
        )


# ---------------------------------------------------------------------------
# _fetch_table_types
# ---------------------------------------------------------------------------


class TestFetchTableTypes(unittest.TestCase):
    def test_returns_empty_when_info_table_is_none(self):
        """No client call must be made when info_table is None."""
        mock_client = MagicMock()
        result = Component._fetch_table_types(mock_client, None)
        self.assertEqual(result, {})
        mock_client.execute_command.assert_not_called()

    def test_returns_empty_string_info_table_falsy(self):
        """Empty string info_table must also return {} without any call."""
        mock_client = MagicMock()
        result = Component._fetch_table_types(mock_client, "")
        self.assertEqual(result, {})
        mock_client.execute_command.assert_not_called()

    def test_parses_fake_info_response_keyed_by_uppercase_name(self):
        """A well-formed INFO response must be parsed into a dict keyed by uppercase column name."""
        mock_client = MagicMock()
        mock_client.execute_command.return_value = [
            {
                "tableStruct": {
                    "tabFields": [
                        {
                            "fiels_name": "CELKEM",
                            "field_type": "decimal",
                            "field_width": 17,
                            "field_decimal": 2,
                            "fields_null": True,
                        }
                    ]
                }
            }
        ]
        result = Component._fetch_table_types(mock_client, "FA_OUT")
        self.assertIn("CELKEM", result)
        self.assertEqual(result["CELKEM"]["field_type"], "decimal")
        self.assertEqual(result["CELKEM"]["field_decimal"], 2)

    def test_key_is_uppercased(self):
        """Column names in the INFO response must be stored under their UPPERCASED key."""
        mock_client = MagicMock()
        mock_client.execute_command.return_value = [
            {
                "tableStruct": {
                    "tabFields": [
                        {
                            "fiels_name": "CelKem",
                            "field_type": "decimal",
                            "field_width": 10,
                            "field_decimal": 0,
                            "fields_null": False,
                        }
                    ]
                }
            }
        ]
        result = Component._fetch_table_types(mock_client, "FA_OUT")
        self.assertIn("CELKEM", result)
        self.assertNotIn("CelKem", result)

    def test_returns_empty_when_client_raises_premier_client_error(self):
        """PremierClientError from INFO must be swallowed and return {}."""
        mock_client = MagicMock()
        mock_client.execute_command.side_effect = PremierClientError("INFO failed")
        result = Component._fetch_table_types(mock_client, "FA_OUT")
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# OBJECT_SPECS sanity check
# ---------------------------------------------------------------------------


class TestObjectSpecsSanity(unittest.TestCase):
    _EXPECTED_KEYS = {
        ObjectType.customers,
        ObjectType.suppliers,
        ObjectType.invoices_issued,
        ObjectType.invoices_received,
        ObjectType.advance_invoices_issued,
        ObjectType.advance_invoices_received,
        ObjectType.orders_received,
        ObjectType.orders_issued,
        ObjectType.products,
        ObjectType.stock_receipts,
        ObjectType.stock_issues,
        ObjectType.stock_levels,
        ObjectType.warehouses,
        ObjectType.vat_rates,
        ObjectType.cost_centers,
        ObjectType.jobs,
        ObjectType.document_series,
    }

    def test_has_exactly_17_entries(self):
        self.assertEqual(len(OBJECT_SPECS), 17)

    def test_all_expected_keys_present(self):
        self.assertEqual(set(OBJECT_SPECS.keys()), self._EXPECTED_KEYS)

    def test_stock_levels_pk_is_cislo_and_sklad(self):
        spec = OBJECT_SPECS[ObjectType.stock_levels]
        self.assertEqual(spec.primary_key, ["cislo", "sklad"])

    def test_vat_rates_pk_is_KOD_DPH(self):
        spec = OBJECT_SPECS[ObjectType.vat_rates]
        self.assertEqual(spec.primary_key, ["KOD_DPH"])

    def test_invoices_issued_info_table_is_FA_OUT(self):
        spec = OBJECT_SPECS[ObjectType.invoices_issued]
        self.assertEqual(spec.info_table, "FA_OUT")

    def test_customers_info_table_is_none(self):
        spec = OBJECT_SPECS[ObjectType.customers]
        self.assertIsNone(spec.info_table)

    def test_suppliers_info_table_is_none(self):
        spec = OBJECT_SPECS[ObjectType.suppliers]
        self.assertIsNone(spec.info_table)


if __name__ == "__main__":
    unittest.main()

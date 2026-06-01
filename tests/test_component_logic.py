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
from unittest.mock import patch

from keboola.component.exceptions import UserException

# Ensure src is importable (already done by tests/__init__.py, but explicit here too)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from component import _TIMESTAMP_FLOOR, Component, ObjectSpec

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
# _collect_columns
# ---------------------------------------------------------------------------


class TestCollectColumns(unittest.TestCase):
    def test_single_record_returns_its_keys(self):
        records = [{"A": 1, "B": 2}]
        self.assertEqual(Component._collect_columns(records), ["A", "B"])

    def test_union_of_keys_across_records(self):
        records = [{"A": 1, "B": 2}, {"A": 3, "C": 4}]
        cols = Component._collect_columns(records)
        self.assertIn("A", cols)
        self.assertIn("B", cols)
        self.assertIn("C", cols)

    def test_sorted_order_returned(self):
        # _collect_columns now returns alphabetically sorted column names, not insertion order.
        records = [{"Z": 1, "A": 2}, {"M": 3, "A": 4}]
        cols = Component._collect_columns(records)
        self.assertEqual(cols, ["A", "M", "Z"])

    def test_no_duplicate_columns(self):
        records = [{"ID": 1}, {"ID": 2}, {"ID": 3}]
        cols = Component._collect_columns(records)
        self.assertEqual(cols, ["ID"])

    def test_empty_records_returns_empty(self):
        self.assertEqual(Component._collect_columns([]), [])

    def test_extra_keys_in_later_records_are_appended(self):
        records = [{"A": 1}, {"A": 2, "B": 3, "C": 4}]
        cols = Component._collect_columns(records)
        self.assertEqual(cols, ["A", "B", "C"])


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

    def test_bool_passthrough(self):
        self.assertEqual(Component._serialize_value(True), True)

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
# _save_records
# ---------------------------------------------------------------------------


class TestSaveRecords(unittest.TestCase):
    def setUp(self):
        self.comp = _make_component()

    def test_skips_output_when_records_empty(self):
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        with patch.object(self.comp, "create_out_table_definition") as mock_create:
            self.comp._save_records([], spec)
        mock_create.assert_not_called()

    def test_raises_user_exception_when_pk_columns_entirely_absent(self):
        """If every PK column is absent from all records, _save_records must raise UserException."""
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"AMOUNT": 100, "DATE": "2024-01-01"}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(UserException) as ctx:
            self.comp._save_records(records, spec)

        self.assertIn("INTER", str(ctx.exception))
        # No output file must have been written.
        self.assertFalse((out_dir / "invoices_issued.csv").exists())

    def test_primary_key_present_when_column_exists(self):
        spec = ObjectSpec("FA_OUT", "invoices_issued", ["INTER"])
        records = [{"INTER": "FA-001", "AMOUNT": 500}]

        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        out_dir.mkdir(parents=True, exist_ok=True)

        self.comp._save_records(records, spec)

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

        self.comp._save_records(records, spec)

        # The component writes headerless CSV (no writeheader call); read raw rows.
        # Columns are sorted alphabetically by _collect_columns: AMOUNT < INTER.
        with open(out_dir / "invoices_issued.csv") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "1000")    # AMOUNT column (sorted first)
        self.assertEqual(rows[0][1], "FA-001")  # INTER column (sorted second)

    def tearDown(self):
        """Clean written output files so tests don't interfere with each other."""
        out_dir = Path(_UNIT_HELPER_DATADIR) / "out" / "tables"
        if out_dir.exists():
            for f in out_dir.glob("*"):
                if f.is_file():
                    f.unlink()


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

    def test_raises_when_username_missing(self):
        comp = self._comp_with_connection(**{"username": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        self.assertIn("username", str(ctx.exception))

    def test_raises_when_password_missing(self):
        comp = self._comp_with_connection(**{"#password": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        self.assertIn("#password", str(ctx.exception))

    def test_raises_when_id_uj_missing(self):
        comp = self._comp_with_connection(**{"id_uj": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        self.assertIn("id_uj", str(ctx.exception))

    def test_raises_lists_all_missing_fields(self):
        comp = self._comp_with_connection(**{"username": "", "id_uj": ""})
        with self.assertRaises(UserException) as ctx:
            comp._build_client()
        msg = str(ctx.exception)
        self.assertIn("username", msg)
        self.assertIn("id_uj", msg)

    def test_returns_client_when_all_fields_present(self):
        from client import PremierClient

        comp = self._comp_with_connection()
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


if __name__ == "__main__":
    unittest.main()

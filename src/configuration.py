"""Pydantic configuration model for the PREMIER System extractor.

Connection/auth lives at the root config level; the object to extract and its
filters live at the row level. The Keboola platform merges the two before the
component runs, so this single model describes the merged shape.

All fields have safe defaults so that sync actions (e.g. ``testConnection``),
which instantiate the configuration before every field is filled in, never fail
during model construction. Presence is validated where it is actually needed.
"""

from enum import StrEnum

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field


class ObjectType(StrEnum):
    """Business objects the extractor can pull, mapped to PREMIER commands in component.py."""

    customers = "customers"
    suppliers = "suppliers"
    invoices_issued = "invoices_issued"
    invoices_received = "invoices_received"
    products = "products"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class Connection(BaseModel):
    """Root-level connection settings (entered once per configuration)."""

    model_config = ConfigDict(populate_by_name=True)

    base_url: str = ""  # host:port of the customer's PREMIER API service
    username: str = ""
    password: str = Field(default="", alias="#password")
    id_uj: str = ""  # accounting-unit GUID sent as the ID-UJ header


class Source(BaseModel):
    """Row-level selection of what to extract."""

    object: ObjectType | None = None
    date_from: str = ""  # yyyy-MM-dd — optional backfill lower bound (invoices only)
    date_to: str = ""  # yyyy-MM-dd — optional backfill upper bound (invoices only)
    warehouse: int | None = None  # PREMIER warehouse number (sklad) — required for products


class Destination(BaseModel):
    load_type: LoadType = LoadType.incremental_load

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load


class Configuration(BaseModel):
    connection: Connection = Field(default_factory=Connection)
    source: Source = Field(default_factory=Source)
    destination: Destination = Field(default_factory=Destination)
    debug: bool = False

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{self._format_location(err.get('loc'))}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}") from e

    @staticmethod
    def _format_location(loc: tuple | None) -> str:
        """Render a Pydantic error location as a dotted path, e.g. connection.#password."""
        if not loc:
            return "root"
        return ".".join(str(part) for part in loc)

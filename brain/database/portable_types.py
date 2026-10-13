"""SQLite variants preserve PostgreSQL column definitions."""
from datetime import timezone

import numpy as np
from sqlalchemy import DateTime, LargeBinary
from sqlalchemy.types import TypeDecorator


class SQLiteUTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def utc_datetime():
    return DateTime(timezone=True).with_variant(SQLiteUTCDateTime(), "sqlite")


class Float32Vector(TypeDecorator):
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        array = np.asarray(value, dtype="<f4")
        if array.ndim != 1 or not np.isfinite(array).all():
            raise ValueError("Vectors must be finite one-dimensional float32 values")
        return array.tobytes()

    def process_result_value(self, value, dialect):
        return np.frombuffer(value, dtype="<f4").tolist() if value is not None else None

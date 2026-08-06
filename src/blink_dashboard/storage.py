"""SQLite persistence for experiment runs and measurements.

The storage layer is synchronous by design: transactions are short and guarded
by a re-entrant lock, while the experiment layer performs all network work
outside of database transactions.  This keeps restart checkpoints atomic without
introducing an additional database dependency.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import threading
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: int
    state: str
    current_test_number: int
    phase_started_at_utc: datetime | None
    phase_deadline_at_utc: datetime | None
    active_elapsed_seconds: float
    stop_reason: str | None
    latest_error: str | None
    snapshots_attempted: int
    snapshots_succeeded: int
    snapshots_failed: int
    snapshot_timeouts: int
    stream_bytes: int
    stream_reconnects: int
    created_at_utc: datetime
    updated_at_utc: datetime
    completed_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class BatteryReading:
    observed_at_utc: datetime
    battery_level_raw: int | float | str | None = None
    battery_state: str | None = None
    battery_voltage_raw: int | float | None = None
    battery_voltage_volts: float | None = None
    blink_battery_check_time: datetime | None = None
    camera_status: str | None = None


@dataclass(frozen=True, slots=True)
class Measurement:
    observed_at_utc: datetime
    observed_at_local: datetime
    state: str
    test_number: int | None = None
    test_name: str | None = None
    snapshot_interval_seconds: float | None = None
    battery_level_raw: int | float | str | None = None
    battery_state: str | None = None
    battery_voltage_raw: int | float | None = None
    battery_voltage_volts: float | None = None
    blink_battery_check_time: datetime | None = None
    camera_status: str | None = None
    snapshots_attempted: int = 0
    snapshots_succeeded: int = 0
    snapshots_failed: int = 0
    snapshot_timeouts: int = 0
    stream_bytes: int = 0
    stream_reconnects: int = 0
    latest_error: str | None = None


_RUN_UPDATE_FIELDS = {
    "state",
    "current_test_number",
    "phase_started_at_utc",
    "phase_deadline_at_utc",
    "active_elapsed_seconds",
    "stop_reason",
    "latest_error",
    "snapshots_attempted",
    "snapshots_succeeded",
    "snapshots_failed",
    "snapshot_timeouts",
    "stream_bytes",
    "stream_reconnects",
    "completed_at_utc",
}
_RUN_DATETIME_FIELDS = {
    "phase_started_at_utc",
    "phase_deadline_at_utc",
    "completed_at_utc",
}


class SQLiteStorage:
    """Versioned SQLite store with WAL and atomic state transitions."""

    def __init__(self, path: str | Path, *, busy_timeout_seconds: float = 5.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_seconds,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_seconds * 1000)}")
        journal_mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).casefold() != "wal":
            raise RuntimeError(f"could not enable SQLite WAL mode (got {journal_mode!r})")
        self._migrate()

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open an immediate transaction and roll it back on any exception."""

        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            with self._lock:
                try:
                    self._connection.executescript(
                        """
                    BEGIN IMMEDIATE;
                    CREATE TABLE runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        state TEXT NOT NULL,
                        current_test_number INTEGER NOT NULL DEFAULT 1
                            CHECK(current_test_number BETWEEN 1 AND 4),
                        phase_started_at_utc TEXT,
                        phase_deadline_at_utc TEXT,
                        active_elapsed_seconds REAL NOT NULL DEFAULT 0
                            CHECK(active_elapsed_seconds >= 0),
                        stop_reason TEXT,
                        latest_error TEXT,
                        snapshots_attempted INTEGER NOT NULL DEFAULT 0,
                        snapshots_succeeded INTEGER NOT NULL DEFAULT 0,
                        snapshots_failed INTEGER NOT NULL DEFAULT 0,
                        snapshot_timeouts INTEGER NOT NULL DEFAULT 0,
                        stream_bytes INTEGER NOT NULL DEFAULT 0,
                        stream_reconnects INTEGER NOT NULL DEFAULT 0,
                        created_at_utc TEXT NOT NULL,
                        updated_at_utc TEXT NOT NULL,
                        completed_at_utc TEXT
                    );

                    CREATE TABLE phases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        test_number INTEGER CHECK(test_number BETWEEN 1 AND 4),
                        phase_kind TEXT NOT NULL,
                        name TEXT NOT NULL,
                        started_at_utc TEXT NOT NULL,
                        ended_at_utc TEXT,
                        outcome TEXT,
                        active_elapsed_at_start REAL,
                        details_json TEXT
                    );

                    CREATE TABLE measurements (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        observed_at_utc TEXT NOT NULL,
                        observed_at_local TEXT NOT NULL,
                        state TEXT NOT NULL,
                        test_number INTEGER,
                        test_name TEXT,
                        snapshot_interval_seconds REAL,
                        battery_level_raw,
                        battery_state TEXT,
                        battery_voltage_raw REAL,
                        battery_voltage_volts REAL,
                        blink_battery_check_time TEXT,
                        camera_status TEXT,
                        snapshots_attempted INTEGER NOT NULL,
                        snapshots_succeeded INTEGER NOT NULL,
                        snapshots_failed INTEGER NOT NULL,
                        snapshot_timeouts INTEGER NOT NULL,
                        stream_bytes INTEGER NOT NULL,
                        stream_reconnects INTEGER NOT NULL,
                        latest_error TEXT
                    );

                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
                        observed_at_utc TEXT NOT NULL,
                        level TEXT NOT NULL,
                        category TEXT NOT NULL,
                        test_number INTEGER,
                        message TEXT NOT NULL,
                        details_json TEXT
                    );

                    CREATE INDEX idx_phases_run_started
                        ON phases(run_id, started_at_utc);
                    CREATE INDEX idx_measurements_run_observed
                        ON measurements(run_id, observed_at_utc);
                    CREATE INDEX idx_events_run_observed
                        ON events(run_id, observed_at_utc DESC);
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                    )
                except BaseException:
                    self._connection.rollback()
                    raise
        elif version == 1:
            with self._lock:
                try:
                    self._connection.executescript(
                        """
                        BEGIN IMMEDIATE;
                        ALTER TABLE runs
                            ADD COLUMN snapshot_timeouts INTEGER NOT NULL DEFAULT 0;
                        ALTER TABLE measurements
                            ADD COLUMN snapshot_timeouts INTEGER NOT NULL DEFAULT 0;
                        PRAGMA user_version = 2;
                        COMMIT;
                        """
                    )
                except BaseException:
                    self._connection.rollback()
                    raise

    def create_run(
        self,
        *,
        state: str = "not_started",
        now: datetime | None = None,
    ) -> RunRecord:
        instant = now or utc_now()
        stamp = to_timestamp(instant)
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO runs(state, created_at_utc, updated_at_utc)
                   VALUES (?, ?, ?)""",
                (state, stamp, stamp),
            )
            run_id = int(cursor.lastrowid)
        result = self.get_run(run_id)
        assert result is not None
        return result

    def get_run(self, run_id: int) -> RunRecord | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row) if row is not None else None

    def latest_run(self) -> RunRecord | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_runs(self) -> list[RunRecord]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM runs ORDER BY id").fetchall()
        return [self._run_from_row(row) for row in rows]

    def update_run(
        self,
        run_id: int,
        *,
        now: datetime | None = None,
        **changes: Any,
    ) -> RunRecord:
        unknown = set(changes) - _RUN_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unsupported run field(s): {', '.join(sorted(unknown))}")
        if not changes:
            existing = self.get_run(run_id)
            if existing is None:
                raise KeyError(f"run {run_id} does not exist")
            return existing
        converted = {
            key: to_timestamp(value) if key in _RUN_DATETIME_FIELDS else value
            for key, value in changes.items()
        }
        converted["updated_at_utc"] = to_timestamp(now or utc_now())
        assignments = ", ".join(f"{key} = ?" for key in converted)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*converted.values(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run {run_id} does not exist")
        result = self.get_run(run_id)
        assert result is not None
        return result

    def begin_phase(
        self,
        run_id: int,
        *,
        phase_kind: str,
        name: str,
        started_at_utc: datetime,
        test_number: int | None = None,
        active_elapsed_at_start: float | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO phases(
                       run_id, test_number, phase_kind, name, started_at_utc,
                       active_elapsed_at_start, details_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    test_number,
                    phase_kind,
                    name,
                    to_timestamp(started_at_utc),
                    active_elapsed_at_start,
                    json.dumps(details, separators=(",", ":"), sort_keys=True)
                    if details is not None
                    else None,
                ),
            )
            return int(cursor.lastrowid)

    def end_phase(
        self,
        phase_id: int,
        *,
        ended_at_utc: datetime,
        outcome: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE phases
                   SET ended_at_utc = ?, outcome = ?,
                       details_json = COALESCE(?, details_json)
                   WHERE id = ? AND ended_at_utc IS NULL""",
                (
                    to_timestamp(ended_at_utc),
                    outcome,
                    json.dumps(details, separators=(",", ":"), sort_keys=True)
                    if details is not None
                    else None,
                    phase_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"open phase {phase_id} does not exist")

    def open_phase(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT * FROM phases
                   WHERE run_id = ? AND ended_at_utc IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
        return self._phase_dict(row) if row is not None else None

    def list_phases(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM phases WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [self._phase_dict(row) for row in rows]

    def add_measurement(self, run_id: int, measurement: Measurement) -> int:
        values = asdict(measurement)
        for key in ("observed_at_utc", "observed_at_local", "blink_battery_check_time"):
            values[key] = to_timestamp(values[key])
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO measurements(run_id, {columns}) VALUES (?, {placeholders})",
                (run_id, *values.values()),
            )
            return int(cursor.lastrowid)

    def list_measurements(
        self,
        run_id: int,
        *,
        start: datetime | None = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        limit = min(limit, 100_000)
        sql = "SELECT * FROM measurements WHERE run_id = ?"
        parameters: list[Any] = [run_id]
        if start is not None:
            sql += " AND observed_at_utc >= ?"
            parameters.append(to_timestamp(start))
        sql += " ORDER BY observed_at_utc, id LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._measurement_dict(row) for row in rows]

    def add_event(
        self,
        *,
        level: str,
        category: str,
        message: str,
        run_id: int | None = None,
        test_number: int | None = None,
        observed_at_utc: datetime | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        if not message.strip():
            raise ValueError("event message cannot be empty")
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO events(
                       run_id, observed_at_utc, level, category, test_number,
                       message, details_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    to_timestamp(observed_at_utc or utc_now()),
                    level,
                    category,
                    test_number,
                    message,
                    json.dumps(details, separators=(",", ":"), sort_keys=True)
                    if details is not None
                    else None,
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, run_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        limit = min(limit, 200)
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM events WHERE run_id = ?
                   ORDER BY observed_at_utc DESC, id DESC LIMIT ?""",
                (run_id, limit),
            ).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            state=row["state"],
            current_test_number=row["current_test_number"],
            phase_started_at_utc=from_timestamp(row["phase_started_at_utc"]),
            phase_deadline_at_utc=from_timestamp(row["phase_deadline_at_utc"]),
            active_elapsed_seconds=row["active_elapsed_seconds"],
            stop_reason=row["stop_reason"],
            latest_error=row["latest_error"],
            snapshots_attempted=row["snapshots_attempted"],
            snapshots_succeeded=row["snapshots_succeeded"],
            snapshots_failed=row["snapshots_failed"],
            snapshot_timeouts=row["snapshot_timeouts"],
            stream_bytes=row["stream_bytes"],
            stream_reconnects=row["stream_reconnects"],
            created_at_utc=from_timestamp(row["created_at_utc"]),  # type: ignore[arg-type]
            updated_at_utc=from_timestamp(row["updated_at_utc"]),  # type: ignore[arg-type]
            completed_at_utc=from_timestamp(row["completed_at_utc"]),
        )

    @staticmethod
    def _phase_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("started_at_utc", "ended_at_utc"):
            result[key] = from_timestamp(result[key])
        details_json = result.pop("details_json")
        result["details"] = json.loads(details_json) if details_json else None
        return result

    @staticmethod
    def _measurement_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("observed_at_utc", "observed_at_local", "blink_battery_check_time"):
            result[key] = from_timestamp(result[key])
        return result

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["observed_at_utc"] = from_timestamp(result["observed_at_utc"])
        details_json = result.pop("details_json")
        result["details"] = json.loads(details_json) if details_json else None
        return result


__all__ = [
    "BatteryReading",
    "Measurement",
    "RunRecord",
    "SCHEMA_VERSION",
    "SQLiteStorage",
    "from_timestamp",
    "to_timestamp",
    "utc_now",
]

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from blink_dashboard.storage import Measurement, SQLiteStorage


NOW = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)


def test_database_uses_wal_and_preserves_run_history(tmp_path):
    storage = SQLiteStorage(tmp_path / "nested" / "experiment.db")
    assert storage.schema_version == 2
    assert storage._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    first = storage.create_run(now=NOW)
    storage.update_run(first.id, state="completed", completed_at_utc=NOW, now=NOW)
    second = storage.create_run(now=NOW)

    assert [run.id for run in storage.list_runs()] == [first.id, second.id]
    assert storage.latest_run().id == second.id


def test_transaction_rolls_back_all_writes(tmp_path):
    storage = SQLiteStorage(tmp_path / "experiment.db")
    with pytest.raises(RuntimeError):
        with storage.transaction() as connection:
            connection.execute(
                "INSERT INTO runs(state, created_at_utc, updated_at_utc) VALUES (?, ?, ?)",
                ("not_started", "2026-08-06T00:00:00Z", "2026-08-06T00:00:00Z"),
            )
            raise RuntimeError("abort")
    assert storage.list_runs() == []


def test_phase_measurement_and_event_round_trip(tmp_path):
    storage = SQLiteStorage(tmp_path / "experiment.db")
    run = storage.create_run(now=NOW)
    phase_id = storage.begin_phase(
        run.id,
        phase_kind="snapshot",
        name="Snapshot every 300 seconds",
        test_number=1,
        started_at_utc=NOW,
        active_elapsed_at_start=2.5,
        details={"resumed": True},
    )
    assert storage.open_phase(run.id)["details"] == {"resumed": True}
    storage.end_phase(phase_id, ended_at_utc=NOW, outcome="completed")

    measurement = Measurement(
        observed_at_utc=NOW,
        observed_at_local=NOW,
        state="running_snapshot",
        test_number=1,
        test_name="Snapshot every 300 seconds",
        snapshot_interval_seconds=300,
        battery_level_raw="3",
        battery_state="ok",
        battery_voltage_raw=165,
        battery_voltage_volts=1.65,
        snapshots_attempted=7,
        snapshots_succeeded=6,
        snapshots_failed=1,
        snapshot_timeouts=2,
    )
    storage.add_measurement(run.id, measurement)
    storage.add_event(
        run_id=run.id,
        observed_at_utc=NOW,
        level="warning",
        category="snapshot",
        message="temporary failure",
        details={"retry": True},
    )

    point = storage.list_measurements(run.id)[0]
    assert point["battery_level_raw"] == "3"
    assert point["battery_voltage_raw"] == 165
    assert point["snapshot_timeouts"] == 2
    assert point["observed_at_utc"] == NOW
    assert storage.list_phases(run.id)[0]["outcome"] == "completed"
    assert storage.list_events(run.id)[0]["details"] == {"retry": True}


def test_constraints_and_update_field_allowlist(tmp_path):
    storage = SQLiteStorage(tmp_path / "experiment.db")
    run = storage.create_run(now=NOW)
    with pytest.raises(ValueError, match="unsupported"):
        storage.update_run(run.id, made_up=True)
    with pytest.raises(sqlite3.IntegrityError):
        storage.update_run(run.id, current_test_number=5)


def test_event_query_is_capped_and_newest_first(tmp_path):
    storage = SQLiteStorage(tmp_path / "experiment.db")
    run = storage.create_run(now=NOW)
    for number in range(205):
        storage.add_event(
            run_id=run.id,
            level="info",
            category="test",
            message=str(number),
            observed_at_utc=NOW,
        )
    events = storage.list_events(run.id, limit=1000)
    assert len(events) == 200
    assert events[0]["message"] == "204"


def test_schema_v1_migrates_timeout_counters_without_losing_rows(tmp_path):
    database = tmp_path / "v1.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            snapshots_attempted INTEGER NOT NULL DEFAULT 0,
            snapshots_succeeded INTEGER NOT NULL DEFAULT 0,
            snapshots_failed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE measurements (
            id INTEGER PRIMARY KEY,
            snapshots_attempted INTEGER NOT NULL DEFAULT 0,
            snapshots_succeeded INTEGER NOT NULL DEFAULT 0,
            snapshots_failed INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO runs VALUES (1, 3, 2, 1);
        INSERT INTO measurements VALUES (1, 3, 2, 1);
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    storage = SQLiteStorage(database)
    assert storage.schema_version == 2
    assert tuple(
        storage._connection.execute(
            "SELECT snapshots_attempted, snapshots_failed, snapshot_timeouts FROM runs"
        ).fetchone()
    ) == (3, 1, 0)
    assert tuple(
        storage._connection.execute(
            "SELECT snapshots_attempted, snapshots_failed, snapshot_timeouts FROM measurements"
        ).fetchone()
    ) == (3, 1, 0)

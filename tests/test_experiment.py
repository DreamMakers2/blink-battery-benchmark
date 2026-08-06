from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from blink_dashboard.config import ExperimentConfig, ExperimentTestConfig, load_config
from blink_dashboard.experiment import (
    ExperimentRunner,
    ExperimentState,
    InvalidTransition,
    SnapshotSchedule,
    normalize_battery_state,
)
from blink_dashboard.storage import BatteryReading, SQLiteStorage


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.base = datetime(2026, 8, 6, tzinfo=timezone.utc)

    def now(self):
        return self.base + timedelta(seconds=self.value)

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.value += max(0.0001, seconds)
        await asyncio.sleep(0)

    def advance(self, seconds):
        self.value += seconds


class FakeBackend:
    def __init__(self, clock, states=("ok",)):
        self.clock = clock
        self.states = deque(states)
        self.last_state = states[-1]
        self.snapshots = 0
        self.snapshot_error = None
        self.stream_connections = 0

    async def capture_snapshot(self):
        if self.snapshot_error:
            if isinstance(self.snapshot_error, BaseException):
                raise self.snapshot_error
            raise RuntimeError(self.snapshot_error)
        self.snapshots += 1

    async def read_battery(self):
        if self.states:
            self.last_state = self.states.popleft()
        if isinstance(self.last_state, Exception):
            raise self.last_state
        return BatteryReading(
            observed_at_utc=self.clock.now(),
            battery_level_raw=3,
            battery_state=self.last_state,
            battery_voltage_raw=165,
            battery_voltage_volts=1.65,
            camera_status="online",
        )

    async def run_stream(self, on_bytes, stop_event, on_reconnect):
        self.stream_connections += 1
        if self.stream_connections > 1:
            on_reconnect()
        while not stop_event.is_set():
            on_bytes(188)
            await asyncio.sleep(0)


def short_config(**changes):
    values = dict(
        test_duration_seconds=0.04,
        recovery_duration_seconds=0.02,
        battery_poll_seconds=0.01,
        measurement_interval_seconds=0.01,
        stream_checkpoint_seconds=0.005,
        fatal_outage_seconds=1,
        tests=(
            ExperimentTestConfig("snap 1", "snapshot", 0.01),
            ExperimentTestConfig("snap 2", "snapshot", 0.01),
            ExperimentTestConfig("snap 3", "snapshot", 0.01),
            ExperimentTestConfig("stream", "stream"),
        ),
    )
    values.update(changes)
    return ExperimentConfig(**values)


async def wait_for(predicate, attempts=1000):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def test_committed_config_loads_and_local_override_wins(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[experiment]\ntest_duration_seconds=12\n[paths]\ndatabase='data/main.db'\n",
        encoding="utf-8",
    )
    (tmp_path / "config.local.toml").write_text(
        "[experiment]\ntest_duration_seconds=2\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    assert config.experiment.test_duration_seconds == 2
    assert config.paths.database == (tmp_path / "data/main.db").resolve()
    assert len(config.experiment.tests) == 4


def test_snapshot_schedule_skips_missed_ticks_without_catchup():
    schedule = SnapshotSchedule(10, 100)
    assert schedule.due(100)
    schedule.advance_after_attempt(135)
    assert schedule.next_deadline == 140
    assert not schedule.due(139.99)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" Needs Replacement ", "needs_replacement"),
        ("REPLACE-BATTERY", "replace_battery"),
        (None, None),
    ],
)
def test_battery_state_normalization(value, expected):
    assert normalize_battery_state(value) == expected


@pytest.mark.asyncio
async def test_four_tests_complete_with_recoveries_and_persist_counters(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    storage = SQLiteStorage(tmp_path / "experiment.db")
    runner = ExperimentRunner(storage, short_config(), backend, clock=clock)

    await runner.start()
    await wait_for(lambda: runner.current_run.state == ExperimentState.COMPLETED.value)

    assert runner.current_run.current_test_number == 4
    assert runner.current_run.snapshots_attempted >= 3
    assert runner.current_run.snapshots_succeeded == runner.current_run.snapshots_attempted
    assert runner.current_run.stream_bytes > 0
    phases = storage.list_phases(runner.current_run.id)
    assert [phase["phase_kind"] for phase in phases].count("recovery") == 3
    assert all(phase["ended_at_utc"] is not None for phase in phases)


@pytest.mark.asyncio
async def test_manual_stop_checkpoints_and_new_runner_resumes_same_test(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    storage = SQLiteStorage(tmp_path / "experiment.db")
    config = short_config(test_duration_seconds=100)
    first = ExperimentRunner(storage, config, backend, clock=clock)
    await first.start()
    clock.advance(7)
    stopped = await first.stop()
    assert stopped.state == ExperimentState.STOPPED_MANUAL.value
    assert stopped.active_elapsed_seconds == pytest.approx(7)

    resumed = ExperimentRunner(storage, config, backend, clock=clock)
    await resumed.initialize()
    assert not resumed.get_status().runner_active
    await resumed.start()
    assert resumed.current_run.current_test_number == 1
    assert resumed.current_run.active_elapsed_seconds == pytest.approx(7)
    await resumed.shutdown()


@pytest.mark.asyncio
async def test_snapshot_active_elapsed_is_durably_checkpointed_between_measurements(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    storage = SQLiteStorage(tmp_path / "experiment.db")
    config = short_config(
        test_duration_seconds=100,
        battery_poll_seconds=100,
        measurement_interval_seconds=100,
        stream_checkpoint_seconds=2,
        tests=(
            ExperimentTestConfig("snap 1", "snapshot", 100),
            ExperimentTestConfig("snap 2", "snapshot", 100),
            ExperimentTestConfig("snap 3", "snapshot", 100),
            ExperimentTestConfig("stream", "stream"),
        ),
    )
    runner = ExperimentRunner(storage, config, backend, clock=clock)
    await runner.start()
    await wait_for(lambda: runner.current_run.active_elapsed_seconds >= 2)
    persisted = storage.get_run(runner.current_run.id)
    assert persisted is not None
    assert persisted.active_elapsed_seconds >= 2
    assert storage.list_measurements(runner.current_run.id)  # start/battery readings only
    await runner.shutdown()


@pytest.mark.asyncio
async def test_low_battery_aborts_current_test_and_continue_requires_fresh_ok(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock, states=("low",))
    storage = SQLiteStorage(tmp_path / "experiment.db")
    runner = ExperimentRunner(
        storage, short_config(recovery_duration_seconds=100), backend, clock=clock
    )
    await runner.start()
    await wait_for(lambda: runner.current_run.state == ExperimentState.STOPPED_LOW_BATTERY.value)
    assert runner.current_run.active_elapsed_seconds == 0
    assert storage.list_phases(runner.current_run.id)[0]["outcome"] == "aborted_low_battery"

    with pytest.raises(InvalidTransition, match="still reported low"):
        await runner.continue_after_low_battery()
    backend.last_state = "ok"
    continued = await runner.continue_after_low_battery()
    assert continued.current_test_number == 2
    assert continued.state == ExperimentState.RUNNING_SNAPSHOT.value
    await runner.shutdown()


@pytest.mark.asyncio
async def test_recovery_deadline_is_wall_clock_and_needs_successful_battery(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock, states=(RuntimeError("offline"), "ok"))
    storage = SQLiteStorage(tmp_path / "experiment.db")
    run = storage.create_run(state=ExperimentState.RECOVERY.value, now=clock.now())
    storage.update_run(
        run.id,
        current_test_number=1,
        phase_deadline_at_utc=clock.now() - timedelta(seconds=1),
        now=clock.now(),
    )
    runner = ExperimentRunner(storage, short_config(), backend, clock=clock)
    await runner.initialize()
    await wait_for(lambda: runner.current_run.current_test_number == 2)
    assert storage.list_events(run.id)[0]["category"] == "battery"
    await runner.shutdown()


@pytest.mark.asyncio
async def test_restart_preserves_old_run_and_prevents_duplicate_start(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    storage = SQLiteStorage(tmp_path / "experiment.db")
    runner = ExperimentRunner(
        storage, short_config(test_duration_seconds=100), backend, clock=clock
    )
    await runner.start()
    with pytest.raises(InvalidTransition):
        await runner.start()
    old_id = runner.current_run.id
    restarted = await runner.restart()
    assert restarted.id != old_id
    assert len(storage.list_runs()) == 2
    assert storage.get_run(old_id).stop_reason == "restarted"
    await runner.shutdown()


@pytest.mark.asyncio
async def test_manual_stop_during_recovery_resumes_recovery_not_completed_test(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    storage = SQLiteStorage(tmp_path / "experiment.db")
    run = storage.create_run(state=ExperimentState.RECOVERY.value, now=clock.now())
    deadline = clock.now() + timedelta(seconds=100)
    storage.update_run(
        run.id,
        current_test_number=1,
        phase_deadline_at_utc=deadline,
        now=clock.now(),
    )
    runner = ExperimentRunner(storage, short_config(), backend, clock=clock)
    await runner.initialize()
    stopped = await runner.stop()
    assert stopped.stop_reason == "manual:recovery"
    assert stopped.phase_deadline_at_utc == deadline

    resumed = await runner.start()
    assert resumed.state == ExperimentState.RECOVERY.value
    assert resumed.current_test_number == 1
    assert resumed.phase_deadline_at_utc == deadline
    await runner.shutdown()


@pytest.mark.asyncio
async def test_snapshot_timeouts_are_counted_separately_from_failures(tmp_path):
    class SnapshotTimeout(RuntimeError):
        category = "snapshot_timeout"

    clock = FakeClock()
    backend = FakeBackend(clock)
    backend.snapshot_error = SnapshotTimeout("request timed out")
    storage = SQLiteStorage(tmp_path / "experiment.db")
    runner = ExperimentRunner(storage, short_config(), backend, clock=clock)

    await runner._capture_snapshot(runner._generation)

    run = runner.current_run
    assert run.snapshots_attempted == 1
    assert run.snapshot_timeouts == 1
    assert run.snapshots_failed == 0
    point = storage.list_measurements(run.id)
    assert point == []  # counter persistence does not depend on measurement cadence


@pytest.mark.asyncio
async def test_non_timeout_snapshot_errors_increment_failure_only(tmp_path):
    clock = FakeClock()
    backend = FakeBackend(clock)
    backend.snapshot_error = RuntimeError("invalid JPEG")
    storage = SQLiteStorage(tmp_path / "experiment.db")
    runner = ExperimentRunner(storage, short_config(), backend, clock=clock)

    await runner._capture_snapshot(runner._generation)

    run = runner.current_run
    assert run.snapshots_attempted == 1
    assert run.snapshots_failed == 1
    assert run.snapshot_timeouts == 0

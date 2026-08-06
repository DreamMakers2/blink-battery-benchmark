"""Restart-safe asynchronous experiment state machine.

Blink and media details deliberately live behind :class:`ExperimentBackend`, so
this module can be exercised deterministically and integrated with either the
real local adapter or a fake adapter.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
import time
from typing import Callable, Protocol, Sequence

from .config import ExperimentConfig
from .storage import BatteryReading, Measurement, RunRecord, SQLiteStorage


class ExperimentState(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING_SNAPSHOT = "running_snapshot"
    RUNNING_STREAM = "running_stream"
    RECOVERY = "recovery"
    COMPLETED = "completed"
    STOPPED_LOW_BATTERY = "stopped_low_battery"
    STOPPED_MANUAL = "stopped_manual"
    STOPPED_ERROR = "stopped_error"


ACTIVE_STATES = {ExperimentState.RUNNING_SNAPSHOT, ExperimentState.RUNNING_STREAM}
RESUMABLE_STATES = ACTIVE_STATES | {
    ExperimentState.RECOVERY,
    ExperimentState.STOPPED_LOW_BATTERY,
}


class InvalidTransition(RuntimeError):
    """Raised when a requested control is not valid for the current state."""


class ExperimentBackend(Protocol):
    async def capture_snapshot(self) -> None:
        """Capture and validate one snapshot, or raise on failure."""

    async def read_battery(self) -> BatteryReading:
        """Return a fresh Blink battery reading, or raise if freshness is unknown."""

    async def run_stream(
        self,
        on_bytes: Callable[[int], None],
        stop_event: asyncio.Event,
        on_reconnect: Callable[[], None],
        on_error: Callable[[str, str], None],
    ) -> None:
        """Run one stream connection until stopped, EOF, or failure."""


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


@dataclass(frozen=True, slots=True)
class ExperimentStatus:
    run: RunRecord
    test_name: str
    test_kind: str
    active_elapsed_seconds: float
    active_remaining_seconds: float | None
    recovery_remaining_seconds: float | None
    runner_active: bool
    stream_receiving: bool
    stream_outage_seconds: float | None
    stream_fatal_outage_seconds: float


class SnapshotSchedule:
    """Monotonic, no-catch-up schedule used by snapshot tests."""

    def __init__(self, interval_seconds: float, anchor: float):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.next_deadline = anchor

    def due(self, now: float) -> bool:
        return now >= self.next_deadline

    def advance_after_attempt(self, now: float) -> None:
        """Advance past every missed tick without queueing extra requests."""

        steps = max(1, math.floor((now - self.next_deadline) / self.interval_seconds) + 1)
        self.next_deadline += steps * self.interval_seconds


def normalize_battery_state(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized or None


class ExperimentRunner:
    """Own one durable run and exactly one background experiment task."""

    def __init__(
        self,
        storage: SQLiteStorage,
        config: ExperimentConfig,
        backend: ExperimentBackend,
        *,
        low_battery_states: Sequence[str] = (
            "low",
            "replace",
            "replace_battery",
            "needs_replacement",
        ),
        clock: Clock | None = None,
    ):
        self.storage = storage
        self.config = config
        if config.stream_data_timeout_seconds <= 0:
            raise ValueError("stream_data_timeout_seconds must be greater than zero")
        if config.stream_data_timeout_seconds > config.fatal_outage_seconds:
            raise ValueError("stream_data_timeout_seconds cannot exceed fatal_outage_seconds")
        self.backend = backend
        self.clock = clock or SystemClock()
        self.low_battery_states = {
            state
            for state in (normalize_battery_state(item) for item in low_battery_states)
            if state
        }
        if not self.low_battery_states:
            raise ValueError("low_battery_states cannot be empty")
        self._lock = asyncio.Lock()
        self._run = storage.latest_run() or storage.create_run(now=self.clock.now())
        self._runner_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._active_anchor: float | None = None
        self._phase_id: int | None = None
        self._last_battery: BatteryReading | None = None
        self._last_measurement_at: float | None = None
        self._outage_started: float | None = None
        self._stream_outage_started: float | None = None
        self._last_stream_bytes_at: float | None = None
        self._pending_stream_valid_seconds = 0.0
        self._pending_stream_errors: list[tuple[str, str]] = []
        self._stream_stop_event: asyncio.Event | None = None
        self._pending_stream_bytes = 0
        self._pending_stream_reconnects = 0

    @property
    def current_run(self) -> RunRecord:
        return self._run

    def get_status(self) -> ExperimentStatus:
        test = self.config.test(self._run.current_test_number)
        state = ExperimentState(self._run.state)
        active_elapsed = self._elapsed_active()
        remaining: float | None = None
        if state in ACTIVE_STATES or state == ExperimentState.STOPPED_MANUAL:
            remaining = max(
                0.0,
                self.config.test_duration(self._run.current_test_number) - active_elapsed,
            )
        recovery_remaining: float | None = None
        if self._run.phase_deadline_at_utc is not None and state in {
            ExperimentState.RECOVERY,
            ExperimentState.STOPPED_LOW_BATTERY,
        }:
            recovery_remaining = max(
                0.0,
                (self._run.phase_deadline_at_utc - self.clock.now()).total_seconds(),
            )
        stream_receiving = self._stream_is_receiving()
        stream_outage = self._stream_outage_duration()
        return ExperimentStatus(
            run=self._run,
            test_name=test.name,
            test_kind=test.kind,
            active_elapsed_seconds=active_elapsed,
            active_remaining_seconds=remaining,
            recovery_remaining_seconds=recovery_remaining,
            runner_active=self._runner_task is not None and not self._runner_task.done(),
            stream_receiving=stream_receiving,
            stream_outage_seconds=stream_outage,
            stream_fatal_outage_seconds=self.config.fatal_outage_seconds,
        )

    async def initialize(self) -> RunRecord:
        """Close an interrupted phase and resume persisted automatic states."""

        async with self._lock:
            state = ExperimentState(self._run.state)
            open_phase = self.storage.open_phase(self._run.id)
            if open_phase is not None:
                self.storage.end_phase(
                    open_phase["id"],
                    ended_at_utc=self.clock.now(),
                    outcome="interrupted",
                )
            if state in ACTIVE_STATES:
                self._begin_active_locked(resumed=True, account_closed_outage=True)
            elif state in {ExperimentState.RECOVERY, ExperimentState.STOPPED_LOW_BATTERY}:
                self._begin_recovery_phase_locked(resumed=True)
            if state in RESUMABLE_STATES:
                self._start_runner_locked()
            return self._run

    async def start(self) -> RunRecord:
        """Start a new unstarted run or resume a manually stopped active test."""

        async with self._lock:
            state = ExperimentState(self._run.state)
            if state == ExperimentState.NOT_STARTED:
                self._run = self.storage.update_run(
                    self._run.id,
                    state=self._active_state().value,
                    stop_reason=None,
                    latest_error=None,
                    phase_deadline_at_utc=None,
                    now=self.clock.now(),
                )
                self._begin_active_locked(resumed=False)
            elif state == ExperimentState.STOPPED_MANUAL:
                origin = (self._run.stop_reason or "").removeprefix("manual:")
                if (
                    origin
                    in {
                        ExperimentState.RECOVERY.value,
                        ExperimentState.STOPPED_LOW_BATTERY.value,
                    }
                    and self._run.phase_deadline_at_utc is not None
                ):
                    self._run = self.storage.update_run(
                        self._run.id,
                        state=origin,
                        stop_reason=(
                            "low_battery"
                            if origin == ExperimentState.STOPPED_LOW_BATTERY.value
                            else None
                        ),
                        now=self.clock.now(),
                    )
                    self._begin_recovery_phase_locked(resumed=True)
                else:
                    self._run = self.storage.update_run(
                        self._run.id,
                        state=self._active_state().value,
                        stop_reason=None,
                        phase_deadline_at_utc=None,
                        now=self.clock.now(),
                    )
                    self._begin_active_locked(resumed=True)
            else:
                raise InvalidTransition(f"cannot start while state is {state.value}")
            self._record_measurement_locked()
            self._start_runner_locked()
            return self._run

    async def stop(self) -> RunRecord:
        """Stop camera activity, preserving elapsed time for later resume."""

        async with self._lock:
            state = ExperimentState(self._run.state)
            if state not in ACTIVE_STATES | {
                ExperimentState.RECOVERY,
                ExperimentState.STOPPED_LOW_BATTERY,
            }:
                raise InvalidTransition(f"cannot stop while state is {state.value}")
            task = self._detach_runner_locked()
            if state in ACTIVE_STATES:
                self._checkpoint_active_locked()
                self._active_anchor = None
            self._end_phase_locked("manual_stop")
            self._run = self.storage.update_run(
                self._run.id,
                state=ExperimentState.STOPPED_MANUAL.value,
                phase_started_at_utc=None,
                phase_deadline_at_utc=(
                    self._run.phase_deadline_at_utc
                    if state
                    in {
                        ExperimentState.RECOVERY,
                        ExperimentState.STOPPED_LOW_BATTERY,
                    }
                    else None
                ),
                stop_reason=f"manual:{state.value}",
                now=self.clock.now(),
            )
            self.storage.add_event(
                run_id=self._run.id,
                test_number=self._run.current_test_number,
                observed_at_utc=self.clock.now(),
                level="info",
                category="experiment",
                message="Experiment stopped manually",
            )
            self._record_measurement_locked()
        await self._await_cancelled(task)
        return self._run

    async def restart(self) -> RunRecord:
        """Preserve the old run and create a fresh run at test one."""

        async with self._lock:
            task = self._detach_runner_locked()
            old_state = ExperimentState(self._run.state)
            if old_state not in {ExperimentState.COMPLETED, ExperimentState.STOPPED_MANUAL}:
                if old_state in ACTIVE_STATES:
                    self._checkpoint_active_locked()
                self._end_phase_locked("restarted")
                self._run = self.storage.update_run(
                    self._run.id,
                    state=ExperimentState.STOPPED_MANUAL.value,
                    stop_reason="restarted",
                    phase_started_at_utc=None,
                    phase_deadline_at_utc=None,
                    now=self.clock.now(),
                )
            self._run = self.storage.create_run(now=self.clock.now())
            self._run = self.storage.update_run(
                self._run.id,
                state=ExperimentState.RUNNING_SNAPSHOT.value,
                now=self.clock.now(),
            )
            self._active_anchor = None
            self._last_battery = None
            self._outage_started = None
            self._reset_stream_tracking_locked()
            self._begin_active_locked(resumed=False)
            self._record_measurement_locked()
            self._start_runner_locked()
        await self._await_cancelled(task)
        return self._run

    async def continue_after_low_battery(self) -> RunRecord:
        """Override only the recovery timer after a fresh non-low reading."""

        async with self._lock:
            if ExperimentState(self._run.state) != ExperimentState.STOPPED_LOW_BATTERY:
                raise InvalidTransition("low-battery continuation is not currently available")
            run_id = self._run.id
            generation = self._generation
        try:
            reading = await self.backend.read_battery()
        except Exception as exc:
            raise InvalidTransition("battery state could not be freshly verified") from exc
        if self._is_low(reading):
            raise InvalidTransition("battery is still reported low")
        async with self._lock:
            if self._run.id != run_id or self._generation != generation:
                raise InvalidTransition("experiment state changed during battery verification")
            self._last_battery = reading
            task = self._detach_runner_locked()
            self._end_phase_locked("continued_early")
            self._advance_after_recovery_locked()
            self._record_measurement_locked()
            if ExperimentState(self._run.state) in ACTIVE_STATES:
                self._start_runner_locked()
        await self._await_cancelled(task)
        return self._run

    async def shutdown(self) -> None:
        """Gracefully checkpoint active time; closed time never consumes it."""

        async with self._lock:
            task = self._detach_runner_locked()
            if ExperimentState(self._run.state) in ACTIVE_STATES:
                self._checkpoint_active_locked()
                self._active_anchor = None
                self._end_phase_locked("paused_shutdown")
                self._run = self.storage.update_run(
                    self._run.id,
                    phase_started_at_utc=None,
                    now=self.clock.now(),
                )
        await self._await_cancelled(task)

    def _start_runner_locked(self) -> None:
        if self._runner_task is not None and not self._runner_task.done():
            return
        generation = self._generation
        self._runner_task = asyncio.create_task(
            self._runner_entry(generation), name=f"experiment-run-{self._run.id}"
        )

    def _detach_runner_locked(self) -> asyncio.Task[None] | None:
        self._generation += 1
        task, self._runner_task = self._runner_task, None
        if self._stream_stop_event is not None:
            self._stream_stop_event.set()
            self._stream_stop_event = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        return task if task is not asyncio.current_task() else None

    @staticmethod
    async def _await_cancelled(task: asyncio.Task[None] | None) -> None:
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    async def _runner_entry(self, generation: int) -> None:
        try:
            while generation == self._generation:
                state = ExperimentState(self._run.state)
                if state == ExperimentState.RUNNING_SNAPSHOT:
                    await self._run_snapshot(generation)
                elif state == ExperimentState.RUNNING_STREAM:
                    await self._run_stream(generation)
                elif state in {ExperimentState.RECOVERY, ExperimentState.STOPPED_LOW_BATTERY}:
                    await self._run_recovery(generation)
                else:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                if generation == self._generation:
                    self._transition_error_locked(f"experiment runner failed: {exc}")

    async def _run_snapshot(self, generation: int) -> None:
        interval = self.config.snapshot_interval(self._run.current_test_number)
        schedule = SnapshotSchedule(interval, self.clock.monotonic())
        next_battery = self.clock.monotonic()
        next_checkpoint = self.clock.monotonic() + self.config.stream_checkpoint_seconds
        next_measurement = self.clock.monotonic() + self.config.measurement_interval_seconds
        while generation == self._generation:
            now_mono = self.clock.monotonic()
            if self._elapsed_active() >= self.config.test_duration(self._run.current_test_number):
                async with self._lock:
                    if generation == self._generation:
                        self._finish_active_locked()
                return
            if now_mono >= next_battery:
                if await self._poll_battery(generation):
                    return
                next_battery = self._advance_deadline(
                    next_battery, self.config.battery_poll_interval_seconds
                )
            now_mono = self.clock.monotonic()
            if schedule.due(now_mono):
                await self._capture_snapshot(generation)
                schedule.advance_after_attempt(self.clock.monotonic())
            if self.clock.monotonic() >= next_checkpoint:
                async with self._lock:
                    if generation != self._generation:
                        return
                    self._checkpoint_active_locked()
                next_checkpoint = self._advance_deadline(
                    next_checkpoint, self.config.stream_checkpoint_seconds
                )
            if self.clock.monotonic() >= next_measurement:
                async with self._lock:
                    if generation != self._generation:
                        return
                    self._checkpoint_active_locked()
                    self._record_measurement_locked()
                next_measurement = self._advance_deadline(
                    next_measurement, self.config.measurement_interval_seconds
                )
            remaining = (
                self.config.test_duration(self._run.current_test_number) - self._elapsed_active()
            )
            delay = min(
                max(0.0, schedule.next_deadline - self.clock.monotonic()),
                max(0.0, next_battery - self.clock.monotonic()),
                max(0.0, next_checkpoint - self.clock.monotonic()),
                max(0.0, next_measurement - self.clock.monotonic()),
                max(0.0, remaining),
            )
            await self.clock.sleep(delay)

    async def _capture_snapshot(self, generation: int) -> None:
        async with self._lock:
            if generation != self._generation:
                return
            self._run = self.storage.update_run(
                self._run.id,
                snapshots_attempted=self._run.snapshots_attempted + 1,
                now=self.clock.now(),
            )
        try:
            await self.backend.capture_snapshot()
        except Exception as exc:
            async with self._lock:
                if generation != self._generation:
                    return
                is_timeout = getattr(exc, "category", None) == "snapshot_timeout"
                counter = (
                    {"snapshot_timeouts": self._run.snapshot_timeouts + 1}
                    if is_timeout
                    else {"snapshots_failed": self._run.snapshots_failed + 1}
                )
                self._run = self.storage.update_run(
                    self._run.id,
                    latest_error=str(exc),
                    now=self.clock.now(),
                    **counter,
                )
                description = "timed out" if is_timeout else "failed"
                self._record_event_locked("warning", "snapshot", f"Snapshot {description}: {exc}")
                self._note_failure_locked(str(exc))
            return
        async with self._lock:
            if generation != self._generation:
                return
            self._run = self.storage.update_run(
                self._run.id,
                snapshots_succeeded=self._run.snapshots_succeeded + 1,
                latest_error=None,
                now=self.clock.now(),
            )
            self._note_success_locked()

    async def _run_stream(self, generation: int) -> None:
        next_battery = self.clock.monotonic()
        next_checkpoint = self.clock.monotonic() + self.config.stream_checkpoint_seconds
        next_measurement = self.clock.monotonic() + self.config.measurement_interval_seconds
        stop_event = asyncio.Event()
        self._stream_stop_event = stop_event
        battery_task: asyncio.Task[BatteryReading] | None = None

        def on_bytes(count: int) -> None:
            if count > 0 and generation == self._generation:
                self._note_stream_bytes(count)

        def on_reconnect() -> None:
            if generation == self._generation:
                self._pending_stream_reconnects += 1

        def on_error(category: str, message: str) -> None:
            if generation == self._generation:
                self._note_stream_error(category, message)

        stream_task = asyncio.create_task(
            self.backend.run_stream(on_bytes, stop_event, on_reconnect, on_error)
        )
        try:
            while generation == self._generation:
                now_mono = self.clock.monotonic()
                self._refresh_stream_outage(now_mono)
                async with self._lock:
                    if generation != self._generation:
                        return
                    self._flush_stream_errors_locked()
                    if self._stream_outage_is_fatal(now_mono):
                        outage = self._stream_outage_duration(now_mono) or 0.0
                        message = (
                            "Continuous stream outage exceeded "
                            f"{self.config.fatal_outage_seconds:g}s "
                            f"({outage:.1f}s without stream data)"
                        )
                        stop_event.set()
                        stream_task.cancel()
                        self._transition_error_locked(message)
                        return
                if self._elapsed_active() >= self.config.test_duration(
                    self._run.current_test_number
                ):
                    stop_event.set()
                    stream_task.cancel()
                    async with self._lock:
                        if generation == self._generation:
                            self._flush_stream_bytes_locked()
                            self._finish_active_locked()
                    return
                if battery_task is not None and battery_task.done():
                    if await self._consume_stream_battery_task(battery_task, generation):
                        stop_event.set()
                        stream_task.cancel()
                        return
                    battery_task = None
                if battery_task is None and now_mono >= next_battery:
                    battery_task = asyncio.create_task(
                        self.backend.read_battery(), name="stream-battery-poll"
                    )
                    next_battery = self._advance_deadline(
                        next_battery, self.config.battery_poll_interval_seconds
                    )
                if self.clock.monotonic() >= next_checkpoint:
                    async with self._lock:
                        if generation != self._generation:
                            return
                        self._flush_stream_bytes_locked()
                        self._checkpoint_active_locked()
                    next_checkpoint = self._advance_deadline(
                        next_checkpoint, self.config.stream_checkpoint_seconds
                    )
                if self.clock.monotonic() >= next_measurement:
                    async with self._lock:
                        if generation != self._generation:
                            return
                        self._flush_stream_bytes_locked()
                        self._record_measurement_locked()
                    next_measurement = self._advance_deadline(
                        next_measurement, self.config.measurement_interval_seconds
                    )
                if stream_task.done():
                    error = stream_task.exception()
                    message = f"Stream disconnected: {error}" if error else "Stream disconnected"
                    async with self._lock:
                        if generation != self._generation:
                            return
                        self._flush_stream_bytes_locked()
                        self._run = self.storage.update_run(
                            self._run.id,
                            stream_reconnects=self._run.stream_reconnects + 1,
                            latest_error=message,
                            now=self.clock.now(),
                        )
                        self._record_event_locked("warning", "stream", message)
                        if not self._run.stream_outage_active:
                            self._start_stream_outage(self.clock.monotonic())
                        if self._stream_outage_is_fatal():
                            self._transition_error_locked(
                                "Continuous stream outage exceeded "
                                f"{self.config.fatal_outage_seconds:g}s: {message}"
                            )
                            return
                    # A backend that exits cannot provide a valid continuous stream.
                    # Restart it immediately; the explicit outage clock remains intact.
                    stream_task = asyncio.create_task(
                        self.backend.run_stream(on_bytes, stop_event, on_reconnect, on_error)
                    )
                await self.clock.sleep(
                    min(
                        0.25,
                        (
                            max(0.0, next_battery - self.clock.monotonic())
                            if battery_task is None
                            else 0.25
                        ),
                        max(0.0, next_checkpoint - self.clock.monotonic()),
                        max(0.0, next_measurement - self.clock.monotonic()),
                    )
                )
        finally:
            stop_event.set()
            if not stream_task.done():
                stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_task
            if battery_task is not None:
                if not battery_task.done():
                    battery_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await battery_task

    async def _consume_stream_battery_task(
        self, task: asyncio.Task[BatteryReading], generation: int
    ) -> bool:
        """Apply a completed stream battery poll without blocking liveness checks."""

        try:
            reading = task.result()
        except asyncio.CancelledError:
            return True
        except Exception as exc:
            async with self._lock:
                if generation != self._generation:
                    return True
                self._run = self.storage.update_run(
                    self._run.id, latest_error=str(exc), now=self.clock.now()
                )
                self._record_event_locked("warning", "battery", f"Battery refresh failed: {exc}")
                fatal = self._note_failure_locked(str(exc))
                self._record_measurement_locked()
                return fatal
        async with self._lock:
            if generation != self._generation:
                return True
            self._last_battery = reading
            self._note_success_locked()
            if self._is_low(reading) and ExperimentState(self._run.state) in ACTIVE_STATES:
                self._transition_low_battery_locked(reading)
                return True
            self._record_measurement_locked()
            return False

    async def _poll_battery(self, generation: int) -> bool:
        try:
            reading = await self.backend.read_battery()
        except Exception as exc:
            async with self._lock:
                if generation != self._generation:
                    return True
                self._run = self.storage.update_run(
                    self._run.id, latest_error=str(exc), now=self.clock.now()
                )
                self._record_event_locked("warning", "battery", f"Battery refresh failed: {exc}")
                fatal = self._note_failure_locked(str(exc))
                self._record_measurement_locked()
                return fatal
        async with self._lock:
            if generation != self._generation:
                return True
            self._last_battery = reading
            self._note_success_locked()
            if self._is_low(reading) and ExperimentState(self._run.state) in ACTIVE_STATES:
                self._transition_low_battery_locked(reading)
                return True
            self._record_measurement_locked()
            return False

    async def _run_recovery(self, generation: int) -> None:
        next_battery = self.clock.monotonic()
        while generation == self._generation:
            deadline = self._run.phase_deadline_at_utc
            if deadline is None:
                async with self._lock:
                    self._transition_error_locked("recovery deadline is missing")
                return
            if self.clock.monotonic() >= next_battery:
                try:
                    reading = await self.backend.read_battery()
                except Exception as exc:
                    async with self._lock:
                        if generation != self._generation:
                            return
                        self._run = self.storage.update_run(
                            self._run.id, latest_error=str(exc), now=self.clock.now()
                        )
                        self._record_event_locked(
                            "warning", "battery", f"Recovery battery refresh failed: {exc}"
                        )
                        self._record_measurement_locked()
                    reading = None
                if reading is not None:
                    async with self._lock:
                        if generation != self._generation:
                            return
                        self._last_battery = reading
                        self._note_success_locked()
                        self._record_measurement_locked()
                        if (
                            self._is_low(reading)
                            and ExperimentState(self._run.state) == ExperimentState.RECOVERY
                        ):
                            self._transition_low_during_recovery_locked(reading)
                            return
                        if self.clock.now() >= deadline and not self._is_low(reading):
                            self._end_phase_locked("completed")
                            self._advance_after_recovery_locked()
                            return
                next_battery = self._advance_deadline(
                    next_battery, self.config.battery_poll_interval_seconds
                )
            until_poll = max(0.0, next_battery - self.clock.monotonic())
            until_deadline = max(0.0, (deadline - self.clock.now()).total_seconds())
            await self.clock.sleep(
                min(until_poll, until_deadline) if until_deadline else until_poll
            )

    def _begin_active_locked(self, *, resumed: bool, account_closed_outage: bool = False) -> None:
        now = self.clock.now()
        test = self.config.test(self._run.current_test_number)
        if test.kind == "stream":
            self._reset_stream_tracking_locked()
            self._restore_or_start_stream_outage_locked(account_closed=account_closed_outage)
            self._active_anchor = None
        else:
            self._active_anchor = self.clock.monotonic()
        self._run = self.storage.update_run(
            self._run.id,
            phase_started_at_utc=now,
            phase_deadline_at_utc=None,
            now=now,
        )
        self._phase_id = self.storage.begin_phase(
            self._run.id,
            phase_kind=test.kind,
            name=test.name,
            test_number=self._run.current_test_number,
            started_at_utc=now,
            active_elapsed_at_start=self._run.active_elapsed_seconds,
            details={"resumed": resumed},
        )

    def _begin_recovery_phase_locked(self, *, resumed: bool) -> None:
        self._active_anchor = None
        state = ExperimentState(self._run.state)
        name = (
            "Low-battery recovery" if state == ExperimentState.STOPPED_LOW_BATTERY else "Recovery"
        )
        self._phase_id = self.storage.begin_phase(
            self._run.id,
            phase_kind="recovery",
            name=name,
            test_number=self._run.current_test_number,
            started_at_utc=self.clock.now(),
            details={"resumed": resumed},
        )

    def _finish_active_locked(self) -> None:
        self._checkpoint_active_locked()
        self._active_anchor = None
        self._end_phase_locked("completed")
        now = self.clock.now()
        if self._run.current_test_number >= len(self.config.tests):
            self._run = self.storage.update_run(
                self._run.id,
                state=ExperimentState.COMPLETED.value,
                active_elapsed_seconds=0.0,
                phase_started_at_utc=None,
                completed_at_utc=now,
                stop_reason=None,
                now=now,
            )
        else:
            deadline = now + timedelta(seconds=self.config.recovery_duration_seconds)
            self._run = self.storage.update_run(
                self._run.id,
                state=ExperimentState.RECOVERY.value,
                active_elapsed_seconds=0.0,
                phase_started_at_utc=now,
                phase_deadline_at_utc=deadline,
                stop_reason=None,
                now=now,
            )
            self._begin_recovery_phase_locked(resumed=False)
        self._record_measurement_locked()

    def _transition_low_battery_locked(self, reading: BatteryReading) -> None:
        self._checkpoint_active_locked()
        self._active_anchor = None
        self._end_phase_locked("aborted_low_battery")
        now = self.clock.now()
        self._run = self.storage.update_run(
            self._run.id,
            state=ExperimentState.STOPPED_LOW_BATTERY.value,
            active_elapsed_seconds=0.0,
            phase_started_at_utc=now,
            phase_deadline_at_utc=now + timedelta(seconds=self.config.recovery_duration_seconds),
            stop_reason="low_battery",
            latest_error=None,
            now=now,
        )
        self._record_event_locked(
            "warning", "battery", f"Low battery reported ({reading.battery_state})"
        )
        self._begin_recovery_phase_locked(resumed=False)
        self._record_measurement_locked()

    def _transition_low_during_recovery_locked(self, reading: BatteryReading) -> None:
        """Turn an inter-test recovery into a full low-battery recovery."""

        self._end_phase_locked("low_battery_detected")
        now = self.clock.now()
        self._run = self.storage.update_run(
            self._run.id,
            state=ExperimentState.STOPPED_LOW_BATTERY.value,
            phase_started_at_utc=now,
            phase_deadline_at_utc=now + timedelta(seconds=self.config.recovery_duration_seconds),
            stop_reason="low_battery",
            latest_error=None,
            now=now,
        )
        self._record_event_locked(
            "warning", "battery", f"Low battery reported during recovery ({reading.battery_state})"
        )
        self._begin_recovery_phase_locked(resumed=False)
        self._record_measurement_locked()

    def _advance_after_recovery_locked(self) -> None:
        now = self.clock.now()
        if self._run.current_test_number >= len(self.config.tests):
            self._run = self.storage.update_run(
                self._run.id,
                state=ExperimentState.COMPLETED.value,
                phase_started_at_utc=None,
                phase_deadline_at_utc=None,
                completed_at_utc=now,
                stop_reason=(
                    "completed_after_low_battery"
                    if self._run.stop_reason == "low_battery"
                    else None
                ),
                now=now,
            )
            return
        next_number = self._run.current_test_number + 1
        next_test = self.config.test(next_number)
        next_state = (
            ExperimentState.RUNNING_SNAPSHOT
            if next_test.kind == "snapshot"
            else ExperimentState.RUNNING_STREAM
        )
        self._run = self.storage.update_run(
            self._run.id,
            state=next_state.value,
            current_test_number=next_number,
            active_elapsed_seconds=0.0,
            phase_started_at_utc=now,
            phase_deadline_at_utc=None,
            stop_reason=None,
            latest_error=None,
            now=now,
        )
        self._begin_active_locked(resumed=False)

    def _transition_error_locked(self, message: str) -> None:
        if ExperimentState(self._run.state) in ACTIVE_STATES:
            self._checkpoint_active_locked()
            self._active_anchor = None
        self._end_phase_locked("error")
        self._run = self.storage.update_run(
            self._run.id,
            state=ExperimentState.STOPPED_ERROR.value,
            phase_started_at_utc=None,
            phase_deadline_at_utc=None,
            stop_reason="error",
            latest_error=message,
            now=self.clock.now(),
        )
        self._record_event_locked("error", "experiment", message)
        self._record_measurement_locked()

    def _checkpoint_active_locked(self) -> None:
        if ExperimentState(self._run.state) == ExperimentState.RUNNING_STREAM:
            elapsed = self._run.active_elapsed_seconds + self._pending_stream_valid_seconds
            self._pending_stream_valid_seconds = 0.0
            self._flush_stream_bytes_locked()
            self._checkpoint_stream_outage_locked()
            self._run = self.storage.update_run(
                self._run.id,
                active_elapsed_seconds=elapsed,
                now=self.clock.now(),
            )
            return
        if self._active_anchor is None:
            return
        now_mono = self.clock.monotonic()
        elapsed = self._run.active_elapsed_seconds + max(0.0, now_mono - self._active_anchor)
        self._active_anchor = now_mono
        self._flush_stream_bytes_locked()
        self._run = self.storage.update_run(
            self._run.id,
            active_elapsed_seconds=elapsed,
            now=self.clock.now(),
        )

    def _flush_stream_bytes_locked(self) -> None:
        if self._pending_stream_bytes or self._pending_stream_reconnects:
            self._run = self.storage.update_run(
                self._run.id,
                stream_bytes=self._run.stream_bytes + self._pending_stream_bytes,
                stream_reconnects=(self._run.stream_reconnects + self._pending_stream_reconnects),
                now=self.clock.now(),
            )
            self._pending_stream_bytes = 0
            self._pending_stream_reconnects = 0

    def _end_phase_locked(self, outcome: str) -> None:
        phase_id, self._phase_id = self._phase_id, None
        if phase_id is not None:
            self.storage.end_phase(phase_id, ended_at_utc=self.clock.now(), outcome=outcome)

    def _record_measurement_locked(self) -> None:
        battery = self._last_battery
        test = self.config.test(self._run.current_test_number)
        now = self.clock.now()
        self.storage.add_measurement(
            self._run.id,
            Measurement(
                observed_at_utc=now,
                observed_at_local=now.astimezone(),
                state=self._run.state,
                test_number=self._run.current_test_number,
                test_name=test.name,
                snapshot_interval_seconds=test.snapshot_interval_seconds,
                battery_level_raw=battery.battery_level_raw if battery else None,
                battery_state=battery.battery_state if battery else None,
                battery_voltage_raw=battery.battery_voltage_raw if battery else None,
                battery_voltage_volts=battery.battery_voltage_volts if battery else None,
                blink_battery_check_time=battery.blink_battery_check_time if battery else None,
                camera_status=battery.camera_status if battery else None,
                snapshots_attempted=self._run.snapshots_attempted,
                snapshots_succeeded=self._run.snapshots_succeeded,
                snapshots_failed=self._run.snapshots_failed,
                snapshot_timeouts=self._run.snapshot_timeouts,
                stream_bytes=self._run.stream_bytes + self._pending_stream_bytes,
                stream_reconnects=self._run.stream_reconnects,
                latest_error=self._run.latest_error,
            ),
        )
        self._last_measurement_at = self.clock.monotonic()

    def _record_event_locked(self, level: str, category: str, message: str) -> None:
        self.storage.add_event(
            run_id=self._run.id,
            test_number=self._run.current_test_number,
            observed_at_utc=self.clock.now(),
            level=level,
            category=category,
            message=message,
        )

    def _note_success_locked(self) -> None:
        self._outage_started = None

    def _reset_stream_tracking_locked(self) -> None:
        self._stream_outage_started = None
        self._last_stream_bytes_at = None
        self._pending_stream_valid_seconds = 0.0
        self._pending_stream_errors.clear()

    def _restore_or_start_stream_outage_locked(self, *, account_closed: bool) -> None:
        """Restore a durable outage, optionally including crash downtime."""

        now_utc = self.clock.now()
        seconds = self._run.stream_outage_seconds
        if self._run.stream_outage_active:
            checkpoint = self._run.stream_outage_checkpoint_at_utc
            if account_closed and checkpoint is not None:
                seconds += max(0.0, (now_utc - checkpoint).total_seconds())
        else:
            seconds = 0.0
        self._stream_outage_started = self.clock.monotonic()
        self._run = self.storage.update_run(
            self._run.id,
            stream_outage_seconds=seconds,
            stream_outage_active=True,
            stream_outage_checkpoint_at_utc=now_utc,
            now=now_utc,
        )

    def _start_stream_outage(self, started_at: float) -> None:
        """Persist the transition into outage before relying on periodic checkpoints."""

        if self._run.stream_outage_active:
            return
        now_mono = self.clock.monotonic()
        now_utc = self.clock.now()
        seconds = max(0.0, now_mono - started_at)
        self._stream_outage_started = now_mono
        self._run = self.storage.update_run(
            self._run.id,
            stream_outage_seconds=seconds,
            stream_outage_active=True,
            stream_outage_checkpoint_at_utc=now_utc,
            now=now_utc,
        )

    def _checkpoint_stream_outage_locked(self) -> None:
        if not self._run.stream_outage_active or self._stream_outage_started is None:
            return
        now_mono = self.clock.monotonic()
        now_utc = self.clock.now()
        seconds = self._run.stream_outage_seconds + max(0.0, now_mono - self._stream_outage_started)
        self._stream_outage_started = now_mono
        self._run = self.storage.update_run(
            self._run.id,
            stream_outage_seconds=seconds,
            stream_outage_active=True,
            stream_outage_checkpoint_at_utc=now_utc,
            now=now_utc,
        )

    def _clear_stream_outage_on_receipt(self) -> None:
        if not self._run.stream_outage_active and self._stream_outage_started is None:
            return
        self._stream_outage_started = None
        self._run = self.storage.update_run(
            self._run.id,
            stream_outage_seconds=0.0,
            stream_outage_active=False,
            stream_outage_checkpoint_at_utc=None,
            now=self.clock.now(),
        )

    def _note_stream_bytes(self, count: int) -> None:
        """Count bytes and only the proven-good interval between nearby receipts."""

        if count <= 0:
            return
        now = self.clock.monotonic()
        if self._last_stream_bytes_at is not None:
            gap = max(0.0, now - self._last_stream_bytes_at)
            if (
                not self._run.stream_outage_active
                and gap <= self.config.stream_data_timeout_seconds
            ):
                self._pending_stream_valid_seconds += gap
        self._last_stream_bytes_at = now
        self._clear_stream_outage_on_receipt()
        self._pending_stream_bytes += int(count)

    def _note_stream_error(self, category: str, message: str) -> None:
        """Make an internal consumer failure visible without waiting for task exit."""

        now = self.clock.monotonic()
        if not self._run.stream_outage_active:
            started_at = (
                self._last_stream_bytes_at if self._last_stream_bytes_at is not None else now
            )
            self._start_stream_outage(started_at)
        self._pending_stream_errors.append((category, message))

    def _stream_is_receiving(self, now: float | None = None) -> bool:
        if ExperimentState(self._run.state) != ExperimentState.RUNNING_STREAM:
            return False
        if self._last_stream_bytes_at is None:
            return False
        current = self.clock.monotonic() if now is None else now
        return (
            max(0.0, current - self._last_stream_bytes_at)
            <= self.config.stream_data_timeout_seconds
            and not self._run.stream_outage_active
        )

    def _refresh_stream_outage(self, now: float | None = None) -> None:
        current = self.clock.monotonic() if now is None else now
        if self._last_stream_bytes_at is None:
            if not self._run.stream_outage_active:
                self._start_stream_outage(current)
            return
        if current - self._last_stream_bytes_at > self.config.stream_data_timeout_seconds:
            if not self._run.stream_outage_active:
                # The whole interval since the last positive receipt is unavailable;
                # the inactivity timeout is only the detector, not valid stream time.
                self._start_stream_outage(self._last_stream_bytes_at)

    def _stream_outage_duration(self, now: float | None = None) -> float | None:
        if ExperimentState(self._run.state) != ExperimentState.RUNNING_STREAM:
            return None
        current = self.clock.monotonic() if now is None else now
        self._refresh_stream_outage(current)
        if not self._run.stream_outage_active:
            return 0.0
        pending = (
            max(0.0, current - self._stream_outage_started)
            if self._stream_outage_started is not None
            else 0.0
        )
        return self._run.stream_outage_seconds + pending

    def _stream_outage_is_fatal(self, now: float | None = None) -> bool:
        duration = self._stream_outage_duration(now)
        return duration is not None and duration >= self.config.fatal_outage_seconds

    def _flush_stream_errors_locked(self) -> None:
        if not self._pending_stream_errors:
            return
        for category, message in self._pending_stream_errors:
            description = f"{category.replace('_', ' ').title()}: {message}"
            self._run = self.storage.update_run(
                self._run.id,
                latest_error=description,
                now=self.clock.now(),
            )
            self._record_event_locked("warning", category, message)
        self._pending_stream_errors.clear()

    def _note_failure_locked(self, message: str) -> bool:
        now = self.clock.monotonic()
        if self._outage_started is None:
            self._outage_started = now
            return False
        if now - self._outage_started >= self.config.fatal_outage_seconds:
            self._transition_error_locked(
                f"Critical adapter outage exceeded {self.config.fatal_outage_seconds:g}s: {message}"
            )
            return True
        return False

    def _is_low(self, reading: BatteryReading) -> bool:
        return normalize_battery_state(reading.battery_state) in self.low_battery_states

    def _active_state(self) -> ExperimentState:
        return (
            ExperimentState.RUNNING_STREAM
            if self.config.test(self._run.current_test_number).kind == "stream"
            else ExperimentState.RUNNING_SNAPSHOT
        )

    def _elapsed_active(self) -> float:
        if ExperimentState(self._run.state) == ExperimentState.RUNNING_STREAM:
            return self._run.active_elapsed_seconds + self._pending_stream_valid_seconds
        if self._active_anchor is None:
            return self._run.active_elapsed_seconds
        return self._run.active_elapsed_seconds + max(
            0.0, self.clock.monotonic() - self._active_anchor
        )

    def _advance_deadline(self, deadline: float, interval: float) -> float:
        now = self.clock.monotonic()
        steps = max(1, math.floor((now - deadline) / interval) + 1)
        return deadline + steps * interval


__all__ = [
    "ACTIVE_STATES",
    "Clock",
    "ExperimentBackend",
    "ExperimentRunner",
    "ExperimentState",
    "ExperimentStatus",
    "InvalidTransition",
    "SnapshotSchedule",
    "SystemClock",
    "normalize_battery_state",
]

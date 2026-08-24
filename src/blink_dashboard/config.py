"""Typed configuration for the dashboard's committed and local TOML files."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True, slots=True)
class ServerConfig:
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8090
    allow_lan: bool = False


@dataclass(frozen=True, slots=True)
class BlinkConfig:
    http_base_url: str = "http://127.0.0.1:8080"
    stream_host: str = "127.0.0.1"
    stream_port: int = 5000
    request_timeout_seconds: float = 120.0
    battery_timeout_seconds: float = 30.0
    stream_connect_timeout_seconds: float = 10.0
    stream_reconnect_seconds: float = 2.0
    low_battery_states: tuple[str, ...] = (
        "low",
        "replace",
        "replace_battery",
        "needs_replacement",
    )


@dataclass(frozen=True, slots=True)
class ExperimentTestConfig:
    name: str
    kind: str
    snapshot_interval_seconds: float | None = None


def _default_tests() -> tuple[ExperimentTestConfig, ...]:
    return (
        ExperimentTestConfig("Snapshot every 300 seconds", "snapshot", 300.0),
        ExperimentTestConfig("Snapshot every 60 seconds", "snapshot", 60.0),
        ExperimentTestConfig("Snapshot every 30 seconds", "snapshot", 30.0),
        ExperimentTestConfig("Continuous live stream", "stream"),
    )


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    test_duration_seconds: float = 43_200.0
    recovery_duration_seconds: float = 43_200.0
    battery_poll_seconds: float = 300.0
    measurement_interval_seconds: float = 300.0
    stream_checkpoint_seconds: float = 5.0
    stream_data_timeout_seconds: float = 20.0
    fatal_outage_seconds: float = 3_600.0
    development_mode: bool = False
    tests: tuple[ExperimentTestConfig, ...] = field(default_factory=_default_tests)

    @property
    def active_test_duration_seconds(self) -> float:
        return self.test_duration_seconds

    @property
    def stream_test_duration_seconds(self) -> float:
        return self.test_duration_seconds

    @property
    def battery_poll_interval_seconds(self) -> float:
        return self.battery_poll_seconds

    @property
    def counter_checkpoint_interval_seconds(self) -> float:
        return self.stream_checkpoint_seconds

    @property
    def fatal_adapter_outage_seconds(self) -> float:
        return self.fatal_outage_seconds

    @property
    def snapshot_intervals_seconds(self) -> tuple[float, ...]:
        return tuple(
            test.snapshot_interval_seconds
            for test in self.tests
            if test.kind == "snapshot" and test.snapshot_interval_seconds is not None
        )

    def test(self, test_number: int) -> ExperimentTestConfig:
        if not 1 <= test_number <= len(self.tests):
            raise ValueError(f"test_number must be 1-{len(self.tests)}, got {test_number}")
        return self.tests[test_number - 1]

    def test_duration(self, test_number: int) -> float:
        self.test(test_number)
        return self.test_duration_seconds

    def snapshot_interval(self, test_number: int) -> float:
        test = self.test(test_number)
        if test.kind != "snapshot" or test.snapshot_interval_seconds is None:
            raise ValueError(f"test {test_number} is not a snapshot test")
        return test.snapshot_interval_seconds


@dataclass(frozen=True, slots=True)
class PathsConfig:
    runtime_dir: Path = Path("runtime")
    database: Path = Path("runtime/data/experiment.db")
    latest_jpeg: Path = Path("runtime/data/latest.jpg")
    hls_dir: Path = Path("runtime/data/hls")
    private_dir: Path = Path("runtime/private")
    log_file: Path = Path("runtime/logs/application.log")


@dataclass(frozen=True, slots=True)
class MediaConfig:
    ffmpeg_executable: str = "ffmpeg"
    hls_segment_seconds: float = 2.0
    hls_list_size: int = 6


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database_path: Path
    busy_timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    blink: BlinkConfig = field(default_factory=BlinkConfig)
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    media: MediaConfig = field(default_factory=MediaConfig)

    @property
    def storage(self) -> StorageConfig:
        return StorageConfig(self.paths.database)

    @property
    def adapter(self) -> BlinkConfig:
        """Compatibility name for code which treats Blink as an adapter."""

        return self.blink


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def _only_known(section: str, values: Mapping[str, Any], known: set[str]) -> None:
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(
            "Unknown configuration setting(s): "
            + ", ".join(f"{section}.{name}" for name in unknown)
        )


def _positive(name: str, value: Any) -> float:
    result = float(value)
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _port(name: str, value: Any) -> int:
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return result


def _normalize_state(value: Any) -> str:
    return str(value).strip().casefold().replace(" ", "_").replace("-", "_")


def _build_config(data: Mapping[str, Any], root: Path) -> AppConfig:
    sections = {"server", "blink", "experiment", "paths", "media"}
    _only_known("root", data, sections)
    raw = {name: dict(data.get(name, {})) for name in sections}
    defaults = AppConfig()
    for name in sections:
        _only_known(name, raw[name], set(getattr(defaults, name).__dataclass_fields__))

    server_data = raw["server"]
    if "dashboard_port" in server_data:
        server_data["dashboard_port"] = _port(
            "server.dashboard_port", server_data["dashboard_port"]
        )

    blink_data = raw["blink"]
    if "stream_port" in blink_data:
        blink_data["stream_port"] = _port("blink.stream_port", blink_data["stream_port"])
    for key in (
        "request_timeout_seconds",
        "battery_timeout_seconds",
        "stream_connect_timeout_seconds",
        "stream_reconnect_seconds",
    ):
        if key in blink_data:
            blink_data[key] = _positive(f"blink.{key}", blink_data[key])
    if "low_battery_states" in blink_data:
        states = tuple(_normalize_state(value) for value in blink_data["low_battery_states"])
        states = tuple(value for value in states if value)
        if not states:
            raise ValueError("blink.low_battery_states cannot be empty")
        blink_data["low_battery_states"] = states

    experiment_data = raw["experiment"]
    tests_data = experiment_data.pop("tests", None)
    for key in (
        "test_duration_seconds",
        "recovery_duration_seconds",
        "battery_poll_seconds",
        "measurement_interval_seconds",
        "stream_checkpoint_seconds",
        "stream_data_timeout_seconds",
        "fatal_outage_seconds",
    ):
        if key in experiment_data:
            experiment_data[key] = _positive(f"experiment.{key}", experiment_data[key])
    data_timeout = experiment_data.get(
        "stream_data_timeout_seconds", defaults.experiment.stream_data_timeout_seconds
    )
    fatal_outage = experiment_data.get(
        "fatal_outage_seconds", defaults.experiment.fatal_outage_seconds
    )
    if data_timeout > fatal_outage:
        raise ValueError(
            "experiment.stream_data_timeout_seconds cannot exceed experiment.fatal_outage_seconds"
        )
    if tests_data is not None:
        tests: list[ExperimentTestConfig] = []
        for index, item in enumerate(tests_data, 1):
            if not isinstance(item, Mapping):
                raise ValueError(f"experiment.tests[{index}] must be a table")
            values = dict(item)
            _only_known(
                f"experiment.tests[{index}]",
                values,
                {"name", "kind", "snapshot_interval_seconds"},
            )
            name = str(values.get("name", "")).strip()
            kind = str(values.get("kind", "")).strip().casefold()
            if not name or kind not in {"snapshot", "stream"}:
                raise ValueError(
                    f"experiment.tests[{index}] requires name and snapshot/stream kind"
                )
            interval = values.get("snapshot_interval_seconds")
            if kind == "snapshot":
                if interval is None:
                    raise ValueError(f"experiment.tests[{index}] snapshot requires an interval")
                interval = _positive(
                    f"experiment.tests[{index}].snapshot_interval_seconds", interval
                )
            elif interval is not None:
                raise ValueError(
                    f"experiment.tests[{index}] stream cannot define snapshot interval"
                )
            tests.append(ExperimentTestConfig(name, kind, interval))
        if len(tests) != 4 or [test.kind for test in tests] != [
            "snapshot",
            "snapshot",
            "snapshot",
            "stream",
        ]:
            raise ValueError(
                "experiment.tests must be three snapshot tests followed by one stream test"
            )
        experiment_data["tests"] = tuple(tests)

    paths_data = raw["paths"]
    for key, value in paths_data.items():
        path = Path(value)
        paths_data[key] = (path if path.is_absolute() else root / path).resolve()
    if not paths_data:
        paths_data = {
            field_name: (root / value).resolve()
            for field_name, value in {
                "runtime_dir": defaults.paths.runtime_dir,
                "database": defaults.paths.database,
                "latest_jpeg": defaults.paths.latest_jpeg,
                "hls_dir": defaults.paths.hls_dir,
                "private_dir": defaults.paths.private_dir,
                "log_file": defaults.paths.log_file,
            }.items()
        }
    else:
        for field_name in defaults.paths.__dataclass_fields__:
            if field_name not in paths_data:
                paths_data[field_name] = (root / getattr(defaults.paths, field_name)).resolve()

    media_data = raw["media"]
    if "hls_segment_seconds" in media_data:
        media_data["hls_segment_seconds"] = _positive(
            "media.hls_segment_seconds", media_data["hls_segment_seconds"]
        )
    if "hls_list_size" in media_data:
        media_data["hls_list_size"] = int(media_data["hls_list_size"])
        if media_data["hls_list_size"] <= 0:
            raise ValueError("media.hls_list_size must be greater than zero")

    return AppConfig(
        server=replace(defaults.server, **server_data),
        blink=replace(defaults.blink, **blink_data),
        experiment=replace(defaults.experiment, **experiment_data),
        paths=replace(defaults.paths, **paths_data),
        media=replace(defaults.media, **media_data),
    )


def load_config(
    project_dir: str | Path | None = None,
    *,
    config_path: str | Path | None = None,
    local_config_path: str | Path | None = None,
) -> AppConfig:
    """Load defaults, then committed config, then local overrides."""

    root = Path(project_dir or Path.cwd()).resolve()
    primary = Path(config_path) if config_path is not None else root / "config.toml"
    local = Path(local_config_path) if local_config_path is not None else root / "config.local.toml"
    return _build_config(_merge(_read_toml(primary), _read_toml(local)), root)


__all__ = [
    "AppConfig",
    "BlinkConfig",
    "ExperimentConfig",
    "ExperimentTestConfig",
    "MediaConfig",
    "PathsConfig",
    "ServerConfig",
    "StorageConfig",
    "load_config",
]

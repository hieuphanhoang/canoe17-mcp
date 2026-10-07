from pathlib import Path

import pytest

from canoe17_mcp.contracts import BackendSettings, Evidence
from canoe17_mcp.settings import Settings, load_settings


def test_toml_and_environment_build_backend_settings(tmp_path: Path) -> None:
    config = tmp_path / "settings.toml"
    config.write_text('read_only = true\n[backend]\nlock_key = "bench"\n', encoding="utf-8")
    settings = load_settings(
        config,
        environ={
            "CANOE17_MCP_READ_ONLY": "false",
            "CANOE17_MCP_OPERATION_DEFAULT_TIMEOUT_S": "12",
            "CANOE17_MCP_CAPL_ALLOWLIST": '["Test"]',
        },
    )
    assert settings.backend == BackendSettings(
        lock_key="bench",
        operation_default_timeout_s=12,
        capl_allowlist=("Test",),
    )
    assert not settings.read_only


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, 3601])
def test_reject_bad_maximum(value: float) -> None:
    with pytest.raises(ValueError):
        Settings(backend=BackendSettings(operation_max_timeout_s=value))


def test_fake_does_not_satisfy_real_evidence_floor() -> None:
    with pytest.raises(ValueError):
        Settings(backend=BackendSettings(min_evidence=Evidence.FAKE))
    assert Evidence.FAKE not in (
        Evidence.DOCUMENTED,
        Evidence.DEMO_VERIFIED,
        Evidence.BENCH_VERIFIED,
    )


def test_roots_are_explicit_and_booleans_strict(tmp_path: Path) -> None:
    assert Settings().allowed_roots == ()
    with pytest.raises(ValueError):
        Settings(allowed_roots=(Path("relative"),))
    with pytest.raises(ValueError):
        load_settings(environ={"CANOE17_MCP_READ_ONLY": '"false"'})
    config = tmp_path / "invalid.toml"
    config.write_text("[backend]\nunknown = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid backend"):
        load_settings(config, environ={})


def test_wait_is_bounded_separately() -> None:
    settings = Settings(
        backend=BackendSettings(operation_default_timeout_s=2, operation_max_timeout_s=4)
    )
    assert settings.wait_seconds(None) == 2
    assert settings.wait_seconds(0) == 0
    with pytest.raises(ValueError):
        settings.wait_seconds(5)


def test_explicit_backend_and_fake_settings(tmp_path: Path) -> None:
    import json

    settings = load_settings(
        environ={
            "CANOE17_MCP_BACKEND_KIND": "fake",
            "CANOE17_MCP_FAKE_CONFIG_PATHS": json.dumps([str(tmp_path / "demo.cfg")]),
            "CANOE17_MCP_FAKE_LICENSED": "true",
        }
    )
    assert settings.backend_kind == "fake" and settings.fake_licensed
    assert settings.fake_config_paths == (tmp_path / "demo.cfg",)
    assert Settings().backend_kind == "com"
    with pytest.raises(ValueError):
        load_settings(environ={"CANOE17_MCP_BACKEND_KIND": "auto"})
    with pytest.raises(ValueError):
        load_settings(environ={"CANOE17_MCP_FAKE_LICENSED": '"true"'})
    with pytest.raises(ValueError):
        Settings(fake_config_paths=(Path("relative.cfg"),))

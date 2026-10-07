from dataclasses import replace
from pathlib import Path

import pytest

from canoe17_mcp import contracts as c
from canoe17_mcp.fake import FakeBackend, FakeConfiguration
from canoe17_mcp.safety import SafetyPolicy
from canoe17_mcp.settings import Settings


def policy(tmp_path: Path, *, licensed: bool = True) -> tuple[FakeBackend, SafetyPolicy]:
    fake = FakeBackend(
        configurations=(
            FakeConfiguration(
                str(tmp_path / "a.cfg"), databases=(c.DatabaseInfo("db:A", "A", "a.dbc", 1, "CAN"),)
            ),
            FakeConfiguration(str(tmp_path / "b.cfg")),
        ),
        licensed=licensed,
    )
    fake.wait(fake.connect().operation_id, 1)
    return fake, SafetyPolicy(fake, Settings(read_only=False, allowed_roots=(tmp_path,)))


def test_read_only_denies_even_preview_and_confirm(tmp_path: Path) -> None:
    fake, _ = policy(tmp_path)
    guard = SafetyPolicy(fake, Settings())
    before = len(fake.transitions)
    guard.check_access(write=False)
    for confirm in (False, True):
        with pytest.raises(c.BackendError) as caught:
            guard.invoke(c.EffectRequest("compile"), confirm=confirm, dispatch=fake.compile)
        assert caught.value.code == c.ErrorCode.READ_ONLY_MODE
    assert len(fake.transitions) == before


def test_preview_has_no_effect_and_reuses_original_epoch(tmp_path: Path) -> None:
    fake, guard = policy(tmp_path)
    request = c.EffectRequest("compile")
    before = len(fake.transitions)
    preview = guard.invoke(request, confirm=False, dispatch=fake.compile)
    assert preview.needs_confirmation and preview.operation is None
    assert len(fake.transitions) == before
    fake.simulate_gui_open(str(tmp_path / "b.cfg"))
    fake.status()  # Unrelated refresh must not authorize the old preview.
    result = guard.invoke(request, confirm=True, dispatch=fake.compile)
    assert result.operation is not None
    done = fake.wait(result.operation.operation_id, 1).value
    assert done.error and done.error.code == c.ErrorCode.STALE_SESSION
    assert not done.dispatched


def test_stale_id_snapshot_cannot_use_new_preview_epoch(tmp_path: Path) -> None:
    fake, guard = policy(tmp_path)
    guard.remember(fake.databases())
    fake.simulate_gui_open(str(tmp_path / "b.cfg"))
    request = c.EffectRequest("database.set_channel", (("database_id", "db:A"), ("channel", 2)))
    with pytest.raises(c.BackendError) as caught:
        guard.invoke(
            request, confirm=True, dispatch=lambda ctx: fake.set_database_channel("db:A", 2, ctx)
        )
    assert caught.value.code == c.ErrorCode.STALE_SESSION


def test_confirm_true_still_requires_explicit_discard(tmp_path: Path) -> None:
    fake, guard = policy(tmp_path)
    fake.simulate_edit(modified=True)
    target = str(tmp_path / "b.cfg")
    result = guard.invoke(
        c.EffectRequest("open_config", (("path", target), ("on_dirty", "refuse"))),
        confirm=True,
        dispatch=lambda ctx: fake.open_config(target, "refuse", False, ctx),
    )
    assert result.operation is not None
    done = fake.wait(result.operation.operation_id, 1).value
    assert done.error and done.error.code == c.ErrorCode.DIRTY_CONFIG
    assert not done.dispatched and fake.status().configuration_modified


def test_paths_use_components_and_check_implicit_save_target(tmp_path: Path) -> None:
    fake, guard = policy(tmp_path)
    assert guard.allowed_path(str(tmp_path / "new.cfg")) == str(tmp_path / "new.cfg")
    for bad in (
        "relative.cfg",
        str(tmp_path) + "-sibling/a.cfg",
        str(tmp_path / "../a.cfg"),
        str(tmp_path / "a.cfg") + ":stream",
    ):
        with pytest.raises(c.BackendError) as caught:
            guard.allowed_path(bad)
        assert caught.value.code == c.ErrorCode.PATH_NOT_ALLOWED
    fake.simulate_edit(path=str(tmp_path.parent / "outside.cfg"), modified=True)
    with pytest.raises(c.BackendError) as caught:
        guard.invoke(
            c.EffectRequest("save_config"),
            confirm=True,
            dispatch=lambda ctx: fake.save_config(None, ctx),
        )
    assert caught.value.code == c.ErrorCode.PATH_NOT_ALLOWED

    with pytest.raises(c.BackendError) as caught:
        guard.invoke(c.EffectRequest("compile"), confirm=True, dispatch=fake.compile)
    assert caught.value.code == c.ErrorCode.PATH_NOT_ALLOWED


@pytest.mark.parametrize(
    "action", ["save_config", "measurement.start", "measurement.stop", "open_config", "quit"]
)
def test_unlicensed_preview_blocks_licensed_effects(tmp_path: Path, action: str) -> None:
    fake, guard = policy(tmp_path, licensed=False)
    fake.simulate_edit(modified=True)
    request = c.EffectRequest(action, (("on_dirty", "save"),))
    before = len(fake.transitions)
    preview = guard.invoke(request, confirm=False, dispatch=fake.compile)
    assert preview.preview.value.blocked_by == c.BlockReason.NO_LICENSE
    with pytest.raises(c.BackendError) as caught:
        guard.invoke(request, confirm=True, dispatch=fake.compile)
    assert caught.value.code == c.ErrorCode.LICENSE_REQUIRED
    assert len(fake.transitions) == before


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        (c.BlockReason.MEASUREMENT_RUNNING, c.ErrorCode.MEASUREMENT_RUNNING),
        (c.BlockReason.MEASUREMENT_STOPPED, c.ErrorCode.MEASUREMENT_NOT_RUNNING),
        (c.BlockReason.NOT_CONNECTED, c.ErrorCode.NOT_CONNECTED),
        (c.BlockReason.NO_CONFIGURATION, c.ErrorCode.NO_CONFIGURATION),
        (c.BlockReason.LOCKED_BY_OTHER_SERVER, c.ErrorCode.LOCKED_BY_OTHER_SERVER),
        (c.BlockReason.NO_LICENSE, c.ErrorCode.LICENSE_REQUIRED),
        (c.BlockReason.DEGRADED, c.ErrorCode.CAPABILITY_UNAVAILABLE),
        (c.BlockReason.UNSUPPORTED, c.ErrorCode.CAPABILITY_UNAVAILABLE),
        (c.BlockReason.MISSING_PREREQUISITE, c.ErrorCode.CAPABILITY_UNAVAILABLE),
    ],
)
def test_blocked_preview_preserves_actionable_error_and_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: c.BlockReason, code: c.ErrorCode
) -> None:
    fake, guard = policy(tmp_path)
    request = c.EffectRequest("compile")
    observed = fake.preview(request)
    monkeypatch.setattr(
        fake,
        "preview",
        lambda _: replace(observed, value=replace(observed.value, blocked_by=reason)),
    )
    before = len(fake.transitions)
    with pytest.raises(c.BackendError) as caught:
        guard.invoke(request, confirm=True, dispatch=fake.compile)
    assert caught.value.code == code
    assert dict(caught.value.info.details)["blocked_by"] == reason.value
    assert len(fake.transitions) == before


@pytest.mark.parametrize("dirty_policy", ["refuse", "discard", "save"])
def test_quit_outside_roots_only_checks_path_when_saving(
    tmp_path: Path, dirty_policy: c.DirtyPolicy
) -> None:
    fake, guard = policy(tmp_path)
    fake.simulate_edit(path=str(tmp_path.parent / "outside.cfg"), modified=True)
    request = c.EffectRequest("quit", (("on_dirty", dirty_policy),))
    if dirty_policy == "save":
        with pytest.raises(c.BackendError) as caught:
            guard.invoke(request, confirm=True, dispatch=lambda ctx: fake.quit(dirty_policy, ctx))
        assert caught.value.code == c.ErrorCode.PATH_NOT_ALLOWED
    else:
        result = guard.invoke(
            request, confirm=True, dispatch=lambda ctx: fake.quit(dirty_policy, ctx)
        )
        assert result.operation is not None
        done = fake.wait(result.operation.operation_id, 1).value
        if dirty_policy == "refuse":
            assert done.error and done.error.code == c.ErrorCode.DIRTY_CONFIG
            assert not done.dispatched
        else:
            assert done.state == c.OpState.COMPLETED
            assert not fake.status().connected

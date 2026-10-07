"""COM session: proxies, event sinks and typed reads. Worker thread only.

Every function here runs on the STA worker. Nothing returned leaves as a COM
object: reads build contract dataclasses. Members missing from the 17.6
early-bound type library (``DiagDescription.Mode``, ``OpenWindows``,
``Node.TestModule``; api-evidence D4/T1) are reached through a late-bound
proxy, which is used for every call. The early-bound proxy exists only to
connect event sinks.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from canoe17_mcp.com.errors import MK_E_UNAVAILABLE, attach_error
from canoe17_mcp.com.ids import Entry, IdRegistry
from canoe17_mcp.com.process import canoe_processes
from canoe17_mcp.contracts import (
    BackendError,
    BusInfo,
    ConfigSummary,
    DatabaseInfo,
    DiagDescriptionInfo,
    ErrorCode,
    NodeInfo,
    TestEnvironmentInfo,
    TestModuleInfo,
    TestSetupInfo,
)

log = logging.getLogger(__name__)

PROGID = "CANoe.Application"
OWN_OPEN_WINDOW_S = 15.0
"""OnOpen arrived about 1.8 s after Open returned (api-evidence C1)."""

DIAG_MODES = {
    0: "interpretation_only",
    1: "tester",
    2: "ecu_simulation",
    3: "physical_request",
    4: "functional_group",
}


def items(collection: Any) -> list[Any]:
    return [collection.Item(i) for i in range(1, int(collection.Count) + 1)]


def late(obj: Any) -> Any:
    """Late-bound view of a COM object. Needed for members missing from the
    17.6 type library; objects returned by CANoe come back early-bound once
    the gen_py cache exists, even when the parent proxy is late-bound."""
    import win32com.client.dynamic as dynamic

    return dynamic.Dispatch(obj._oleobj_)


class Inbox:
    """Event sinks append here (during message pumping); the worker drains it."""

    def __init__(self) -> None:
        self.events: list[tuple[float, str, tuple[Any, ...]]] = []

    def push(self, name: str, *args: Any) -> None:
        self.events.append((time.monotonic(), name, tuple(str(a) for a in args)))

    def drain(self) -> list[tuple[float, str, tuple[Any, ...]]]:
        out, self.events = self.events, []
        return out


def _make_sinks(inbox: Inbox) -> tuple[type, type]:
    class AppSink:
        def OnOpen(self, fullname: Any) -> None:  # noqa: N802 - COM event name
            inbox.push("App.OnOpen", fullname)

        def OnQuit(self) -> None:  # noqa: N802
            inbox.push("App.OnQuit")

    class MeasurementSink:
        def OnInit(self) -> None:  # noqa: N802
            inbox.push("Meas.OnInit")

        def OnStart(self) -> None:  # noqa: N802
            inbox.push("Meas.OnStart")

        def OnStopRequested(self) -> None:  # noqa: N802
            inbox.push("Meas.OnStopRequested")

        def OnStop(self) -> None:  # noqa: N802
            inbox.push("Meas.OnStop")

        def OnExit(self) -> None:  # noqa: N802
            inbox.push("Meas.OnExit")

    return AppSink, MeasurementSink


class ComSession:
    """Owns the CANoe proxies. Create and use only on the worker thread."""

    def __init__(self) -> None:
        self.app: Any = None  # late-bound
        self._early: Any = None
        self._sinks: list[Any] = []
        self.inbox = Inbox()
        self.ids = IdRegistry()
        self.launched = False
        self._own_open: tuple[str, float] | None = None

    # -------------------------------------------------------------- connection

    @property
    def connected(self) -> bool:
        return self.app is not None

    def attach(self, *, launch: bool) -> bool:
        """Attach to a running CANoe, or launch one if ``launch``.

        Returns True when attached, False when launched. The ROT is empty for
        CANoe 17 (api-evidence A2), so presence is a process check; Dispatch
        then attaches to that instance (A3) or launches (A4).
        """
        import pythoncom
        import pywintypes
        import win32com.client
        import win32com.client.dynamic as dynamic
        import win32com.client.gencache as gencache

        running = canoe_processes()
        if not running and not launch:
            raise BackendError(
                ErrorCode.NO_ACTIVE_INSTANCE,
                "CANoe is not running. Open a configuration with launch_if_absent=true "
                "to start it.",
                hresult=MK_E_UNAVAILABLE,
            )
        try:
            early = gencache.EnsureDispatch(PROGID)
        except pywintypes.com_error as exc:
            if running:
                hresult, _text, excepinfo = (tuple(exc.args) + (None, None, None))[:3]
                raise BackendError.from_info(attach_error(int(hresult or 0), excepinfo)) from exc
            raise
        self._early = early
        self.app = dynamic.Dispatch(early._oleobj_)
        self.launched = not running
        app_sink, meas_sink = _make_sinks(self.inbox)
        self._sinks = [
            win32com.client.WithEvents(early, app_sink),
            win32com.client.WithEvents(early.Measurement, meas_sink),
        ]
        pythoncom.PumpWaitingMessages()
        return bool(running)

    def detach(self) -> None:
        for sink in self._sinks:
            try:
                sink.close()  # Unadvise now, not in __del__ during CoUninitialize
            except Exception:  # noqa: BLE001 - CANoe may already be gone
                pass
        self._sinks = []
        self.app = None
        self._early = None
        self.ids.clear()

    # ----------------------------------------------------------------- events

    def expect_own_open(self, path: str) -> None:
        self._own_open = (path.lower(), time.monotonic() + OWN_OPEN_WINDOW_S)

    def is_own_open(self, path: str) -> bool:
        """True (once) for the OnOpen our own open/save triggered."""
        own = self._own_open
        if own is None:
            return False
        if time.monotonic() > own[1]:
            self._own_open = None
            return False
        if path.lower() == own[0]:
            self._own_open = None
            return True
        return False

    # ------------------------------------------------------------------ reads

    def session_fields(self) -> dict[str, Any]:
        app = self.app
        cfg = app.Configuration
        version = app.Version
        return {
            "canoe_version": f"{version.major}.{version.minor}.{version.Build}",
            "canoe_exe": str(app.FullName),
            "configuration_path": str(cfg.FullName) or None,
            "configuration_modified": bool(cfg.Modified),
            "measurement_running": bool(app.Measurement.Running),
        }

    def cheap_fields(self) -> dict[str, Any]:
        app = self.app
        cfg = app.Configuration
        return {
            "configuration_path": str(cfg.FullName) or None,
            "configuration_modified": bool(cfg.Modified),
            "measurement_running": bool(app.Measurement.Running),
        }

    def require_configuration(self) -> Any:
        cfg = self.app.Configuration
        if not str(cfg.FullName):
            raise BackendError(ErrorCode.NO_CONFIGURATION, "No configuration is open in CANoe.")
        return cfg

    def bus_objects(self) -> list[Any]:
        return items(self.require_configuration().SimulationSetup.Buses)

    def database_objects(self) -> list[tuple[Any, str]]:
        """(database, bus name) pairs in Simulation Setup bus order."""
        out = []
        for bus in self.bus_objects():
            for db in items(bus.Databases):
                out.append((db, str(bus.Name)))
        return out

    def database_entries(self) -> list[Entry]:
        return [Entry(str(db.Name), str(db.FullName)) for db, _ in self.database_objects()]

    def databases(self, epoch: int) -> tuple[DatabaseInfo, ...]:
        objs = self.database_objects()
        entries = [Entry(str(db.Name), str(db.FullName)) for db, _ in objs]
        ids = self.ids.issue("databases", epoch, entries)
        out = []
        for i, (db, bus) in enumerate(objs):
            channel = _safe(lambda d=db: int(d.Channel))
            out.append(DatabaseInfo(ids[i], entries[i].name, entries[i].path, channel, bus))
        return tuple(out)

    def buses(self, epoch: int) -> tuple[BusInfo, ...]:
        objs = self.bus_objects()
        entries = [Entry(str(b.Name)) for b in objs]
        ids = self.ids.issue("buses", epoch, entries)
        return tuple(
            BusInfo(ids[i], entries[i].name, str(_safe(lambda b=b: b.BusType) or "CAN"))
            for i, b in enumerate(objs)
        )

    def node_objects(self) -> list[Any]:
        return items(self.require_configuration().SimulationSetup.Nodes)

    def nodes(self, epoch: int) -> tuple[NodeInfo, ...]:
        objs = self.node_objects()
        entries = [Entry(str(n.Name), str(_safe(lambda n=n: n.FullName) or "")) for n in objs]
        ids = self.ids.issue("nodes", epoch, entries)
        out = []
        for i, n in enumerate(objs):
            out.append(
                NodeInfo(
                    id=ids[i],
                    name=entries[i].name,
                    active=_safe(lambda n=n: bool(n.Active)),
                    capl_path=entries[i].path or None,
                    test_module=bool(_safe(lambda n=n: late(n).TestModule)),
                )
            )
        return tuple(out)

    def diag_objects(self) -> list[Any]:
        cfg = self.require_configuration()
        return items(cfg.GeneralSetup.DiagnosticsSetup.DiagDescriptions)

    def diag_entries(self) -> list[Entry]:
        return [Entry(str(d.Qualifier), str(d.FilePath)) for d in self.diag_objects()]

    def diag_descriptions(self, epoch: int) -> tuple[DiagDescriptionInfo, ...]:
        objs = self.diag_objects()
        entries = [Entry(str(d.Qualifier), str(d.FilePath)) for d in objs]
        ids = self.ids.issue("diag_descriptions", epoch, entries)
        out = []
        for i, d in enumerate(objs):
            mode_raw = _safe(lambda d=d: int(late(d).Mode))
            out.append(
                DiagDescriptionInfo(
                    id=ids[i],
                    qualifier=entries[i].name,
                    network=str(_safe(lambda d=d: d.Network) or ""),
                    node=str(_safe(lambda d=d: d.Node) or "") or None,
                    file_path=entries[i].path,
                    mode=DIAG_MODES.get(mode_raw, "other") if mode_raw is not None else "other",  # type: ignore[arg-type]
                )
            )
        return tuple(out)

    def test_setup(self, epoch: int) -> TestSetupInfo:
        cfg = self.require_configuration()
        envs = items(cfg.TestSetup.TestEnvironments)
        env_entries = [Entry(str(e.Name), str(_safe(lambda e=e: e.FullName) or "")) for e in envs]
        env_ids = self.ids.issue("test_environments", epoch, env_entries)
        env_infos = []
        for i, env in enumerate(envs):
            modules = []
            for m in items(env.TestModules):
                name = str(m.Name)
                modules.append(
                    TestModuleInfo(
                        id=f"tm:{env_ids[i][len('env:'):]}/{name}",
                        name=name,
                        path=str(_safe(lambda m=m: m.FullName) or "") or None,
                        enabled=_safe(lambda m=m: bool(m.Enabled)),
                        start_on_measurement=None,
                    )
                )
            env_infos.append(
                TestEnvironmentInfo(
                    id=env_ids[i],
                    name=env_entries[i].name,
                    path=env_entries[i].path or None,
                    enabled=_safe(lambda e=env: bool(e.Enabled)),
                    modules=tuple(modules),
                )
            )
        sim = [n for n in self.node_objects() if _safe(lambda n=n: bool(late(n).TestModule))]
        sim_entries = [Entry(str(n.Name), str(_safe(lambda n=n: n.FullName) or "")) for n in sim]
        sim_ids = self.ids.issue("simulation_test_nodes", epoch, sim_entries)
        sim_infos = tuple(
            TestModuleInfo(
                id=sim_ids[i],
                name=sim_entries[i].name,
                path=sim_entries[i].path or None,
                enabled=_safe(lambda n=n: bool(n.Active)),
                start_on_measurement=True,
                source="simulation_setup",
                startable=False,
            )
            for i, n in enumerate(sim)
        )
        return TestSetupInfo(environments=tuple(env_infos), simulation_test_nodes=sim_infos)

    def summary(self, epoch: int, section: str) -> ConfigSummary:
        path = str(self.require_configuration().FullName)

        def want(name: str) -> bool:
            return section in ("all", name)

        return ConfigSummary(
            configuration_path=path,
            buses=self.buses(epoch) if want("networks") else (),
            databases=self.databases(epoch) if want("databases") else (),
            nodes=self.nodes(epoch) if want("nodes") else (),
            diag_descriptions=self.diag_descriptions(epoch) if want("diagnostics") else (),
            test_setup=self.test_setup(epoch) if want("tests") else None,
        )

    def write_window_text(self) -> str:
        return str(self.app.UI.Write.Text)


def _safe[T](fn: Callable[[], T]) -> T | None:
    """Read an optional property; None when CANoe does not provide it."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional COM property
        return None

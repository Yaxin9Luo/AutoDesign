"""Durable spawn registration and verified process-tree termination."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager, contextmanager
import asyncio
from dataclasses import asdict, dataclass
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import select
import struct
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
from uuid import uuid4

from .run_control import durable_replace_json


_LEDGER_VERSION = 1
_FRAME = struct.Struct(">I")
_PROCESS_OWNER_ENV = "AUTODESIGN_PROCESS_OWNER"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    birth_id: str
    process_group_id: int | None
    parent_pid: int | None


@dataclass(frozen=True)
class ProcessRecord:
    identity: ProcessIdentity
    role: str
    nonce: str
    registered_at: float


@dataclass(frozen=True)
class SpawnIntent:
    nonce: str
    role: str
    status: str
    created_at: float
    failure: str | None = None
    shim_identity: ProcessIdentity | None = None
    shim_pid: int | None = None


@dataclass(frozen=True)
class LedgerSnapshot:
    sealed: bool
    processes: tuple[ProcessRecord, ...]
    spawning: tuple[SpawnIntent, ...]


@dataclass(frozen=True)
class TerminationReport:
    terminated: tuple[ProcessIdentity, ...]
    survivors: tuple[ProcessIdentity, ...]
    stale_identities: tuple[ProcessIdentity, ...]
    unresolved_spawns: tuple[str, ...]


class ProcessLedgerError(RuntimeError):
    """The durable process ownership ledger could not be trusted."""


def _configure_windows_api(kernel32: Any, ntdll: Any | None = None) -> None:
    """Declare every Windows process/Job signature before passing 64-bit handles."""
    import ctypes
    from ctypes import wintypes

    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    if ntdll is not None:
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long


class WindowsJob:
    """Kill-on-close Job Object for one supervised worker tree."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are available only on Windows")
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_windows_api(kernel32)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        self._kernel32 = kernel32
        self._handle = handle

    def assign(self, pid: int) -> None:
        import ctypes
        process = self._kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        import ctypes
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def resume_windows_process(pid: int) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _configure_windows_api(kernel32, ntdll)
    handle = kernel32.OpenProcess(0x0800, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        status = ntdll.NtResumeProcess(handle)
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS {status:#x}")
    finally:
        kernel32.CloseHandle(handle)


class ProcessLedger:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "process_ledger.json"
        self.lock_path = self.run_dir / ".spawn.lock"

    @contextmanager
    def exclusive(self, *, timeout_s: float = 5.0) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        key = str(self.lock_path.resolve())
        with _LOCKS_GUARD:
            local_lock = _LOCKS.setdefault(key, threading.RLock())
        with local_lock:
            with self.lock_path.open("a+b") as handle:
                _lock_file(handle, timeout_s=timeout_s)
                try:
                    yield
                finally:
                    _unlock_file(handle)

    @asynccontextmanager
    async def async_exclusive(self, *, timeout_s: float = 5.0) -> Any:
        """Acquire the cross-process lock without blocking an asyncio loop."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            await asyncio.to_thread(_lock_file, handle, timeout_s=timeout_s)
            try:
                yield
            finally:
                await asyncio.to_thread(_unlock_file, handle)
        finally:
            handle.close()

    def read(self) -> LedgerSnapshot:
        with self.exclusive():
            return self._read_unlocked()

    def begin_spawn(self, role: str, nonce: str | None = None) -> str:
        with self.exclusive():
            return self.begin_spawn_unlocked(role, nonce)

    def begin_spawn_unlocked(self, role: str, nonce: str | None = None) -> str:
        snapshot = self._read_unlocked()
        if snapshot.sealed:
            raise ProcessLedgerError("process ledger is sealed")
        spawn_nonce = nonce or uuid4().hex
        if any(item.nonce == spawn_nonce for item in snapshot.spawning):
            raise ProcessLedgerError(f"duplicate spawn nonce: {spawn_nonce}")
        spawning = (*snapshot.spawning, SpawnIntent(
            nonce=spawn_nonce,
            role=str(role),
            status="spawning",
            created_at=time.time(),
        ))
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, snapshot.processes, spawning))
        return spawn_nonce

    def register_unlocked(
        self,
        identity: ProcessIdentity,
        *,
        role: str,
        nonce: str,
    ) -> ProcessRecord:
        snapshot = self._read_unlocked()
        intent = next((item for item in snapshot.spawning if item.nonce == nonce), None)
        if intent is None or intent.status != "spawning":
            raise ProcessLedgerError(f"spawn intent is not active: {nonce}")
        if any(record.identity.pid == identity.pid for record in snapshot.processes):
            raise ProcessLedgerError(f"PID already registered: {identity.pid}")
        record = ProcessRecord(identity, str(role), nonce, time.time())
        spawning = tuple(
            SpawnIntent(
                item.nonce, item.role, "registered", item.created_at,
                item.failure, item.shim_identity, item.shim_pid,
            )
            if item.nonce == nonce else item
            for item in snapshot.spawning
        )
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, (*snapshot.processes, record), spawning))
        return record

    def fail_spawn_unlocked(self, nonce: str, failure: str) -> None:
        snapshot = self._read_unlocked()
        spawning = tuple(
            SpawnIntent(
                item.nonce, item.role, "failed", item.created_at,
                str(failure)[:300], item.shim_identity, item.shim_pid,
            )
            if item.nonce == nonce and item.status == "spawning" else item
            for item in snapshot.spawning
        )
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, snapshot.processes, spawning))

    def rollback_spawn_unlocked(self, nonce: str, failure: str) -> None:
        """Retire one unreleased nonce while retaining ownership history."""
        snapshot = self._read_unlocked()
        spawning = tuple(
            SpawnIntent(
                item.nonce, item.role, "failed", item.created_at,
                str(failure)[:300], item.shim_identity, item.shim_pid,
            )
            if item.nonce == nonce else item
            for item in snapshot.spawning
        )
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, snapshot.processes, spawning))

    def record_shim_unlocked(self, nonce: str, identity: ProcessIdentity) -> None:
        snapshot = self._read_unlocked()
        found = False
        spawning: list[SpawnIntent] = []
        for item in snapshot.spawning:
            if item.nonce == nonce and item.status == "spawning":
                found = True
                spawning.append(SpawnIntent(
                    item.nonce, item.role, item.status, item.created_at,
                    item.failure, identity, item.shim_pid,
                ))
            else:
                spawning.append(item)
        if not found:
            raise ProcessLedgerError(f"spawn intent is not active: {nonce}")
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, snapshot.processes, tuple(spawning)))

    def record_shim_pid_unlocked(self, nonce: str, pid: int) -> None:
        snapshot = self._read_unlocked()
        found = False
        spawning: list[SpawnIntent] = []
        for item in snapshot.spawning:
            if item.nonce == nonce and item.status == "spawning":
                found = True
                spawning.append(SpawnIntent(
                    item.nonce, item.role, item.status, item.created_at,
                    item.failure, item.shim_identity, int(pid),
                ))
            else:
                spawning.append(item)
        if not found:
            raise ProcessLedgerError(f"spawn intent is not active: {nonce}")
        self._write_unlocked(LedgerSnapshot(snapshot.sealed, snapshot.processes, tuple(spawning)))

    def reconcile_abandoned_spawns(self, *, grace_s: float = 0.2) -> TerminationReport:
        """Reap release-blocked shims left by a crashed spawning process."""
        with self.exclusive():
            snapshot = self._read_unlocked()
            active = tuple(item for item in snapshot.spawning if item.status == "spawning")
            identities = tuple(
                item.shim_identity for item in active if item.shim_identity is not None
            )
        # Before release a shim may still share its caller's process group.
        # Reconcile only its birth-verified PID; that group is not owned.
        isolated_identities = tuple(
            ProcessIdentity(identity.pid, identity.birth_id, None, identity.parent_pid)
            for identity in identities
        )
        report = terminate_process_identities(
            isolated_identities, root_pid=None, grace_s=grace_s,
            unresolved_spawns=tuple(item.nonce for item in active if item.shim_identity is None),
        )
        unknown = tuple(item for item in active if item.shim_identity is None)
        if unknown:
            deadline = time.monotonic() + max(0.0, min(grace_s, 1.0))
            while time.monotonic() < deadline and any(
                item.shim_pid is not None and _pid_exists_unverified(item.shim_pid)
                for item in unknown
            ):
                time.sleep(0.01)
        live_survivors = tuple(
            identity for identity in report.survivors if process_is_alive(identity)
        )
        with self.exclusive():
            for item in active:
                identity = item.shim_identity
                unknown_pid_gone = (
                    identity is None
                    and (item.shim_pid is None or not _pid_exists_unverified(item.shim_pid))
                )
                if unknown_pid_gone or (identity is not None and not process_is_alive(identity)):
                    self.fail_spawn_unlocked(item.nonce, "abandoned spawn reconciled")
            remaining = self._read_unlocked()
            unresolved = tuple(
                item.nonce for item in remaining.spawning if item.status == "spawning"
            )
        return TerminationReport(
            terminated=report.terminated,
            survivors=live_survivors,
            stale_identities=report.stale_identities,
            unresolved_spawns=unresolved,
        )

    def register_existing(
        self,
        identity: ProcessIdentity,
        *,
        role: str,
        nonce: str | None = None,
    ) -> ProcessRecord:
        with self.exclusive():
            spawn_nonce = self.begin_spawn_unlocked(role, nonce)
            return self.register_unlocked(identity, role=role, nonce=spawn_nonce)

    def seal_unlocked(self) -> LedgerSnapshot:
        snapshot = self._read_unlocked()
        if not snapshot.sealed:
            snapshot = LedgerSnapshot(True, snapshot.processes, snapshot.spawning)
            self._write_unlocked(snapshot)
        return snapshot

    def _read_unlocked(self) -> LedgerSnapshot:
        if not self.path.exists():
            return LedgerSnapshot(False, (), ())
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessLedgerError(f"invalid process ledger: {self.path}") from exc
        if set(payload) != {"version", "sealed", "processes", "spawning"}:
            raise ProcessLedgerError(f"unexpected process ledger fields: {self.path}")
        if payload["version"] != _LEDGER_VERSION or type(payload["sealed"]) is not bool:
            raise ProcessLedgerError(f"unsupported process ledger: {self.path}")
        try:
            processes = tuple(
                ProcessRecord(
                    identity=ProcessIdentity(**item["identity"]),
                    role=item["role"],
                    nonce=item["nonce"],
                    registered_at=float(item["registered_at"]),
                )
                for item in payload["processes"]
            )
            spawning = tuple(
                SpawnIntent(
                    nonce=item["nonce"], role=item["role"], status=item["status"],
                    created_at=float(item["created_at"]), failure=item.get("failure"),
                    shim_identity=(
                        ProcessIdentity(**item["shim_identity"])
                        if item.get("shim_identity") is not None else None
                    ),
                    shim_pid=(int(item["shim_pid"]) if item.get("shim_pid") is not None else None),
                )
                for item in payload["spawning"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProcessLedgerError(f"malformed process ledger: {self.path}") from exc
        return LedgerSnapshot(payload["sealed"], processes, spawning)

    def _write_unlocked(self, snapshot: LedgerSnapshot) -> None:
        durable_replace_json(
            self.path,
            {
                "version": _LEDGER_VERSION,
                "sealed": snapshot.sealed,
                "processes": [asdict(record) for record in snapshot.processes],
                "spawning": [asdict(intent) for intent in snapshot.spawning],
            },
        )


def process_identity(pid: int) -> ProcessIdentity:
    if pid <= 1:
        raise ProcessLookupError(pid)
    if os.name == "posix":
        if sys.platform.startswith("linux"):
            linux_identity = _linux_process_identity(pid)
            if linux_identity is not None:
                return linux_identity
        if sys.platform == "darwin":
            darwin_identity = _darwin_process_identity(pid)
            if darwin_identity is not None:
                return darwin_identity
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "pgid=", "-o", "state=", "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        line = completed.stdout.strip()
        if completed.returncode != 0 or not line:
            raise ProcessLookupError(pid)
        parts = line.split(None, 3)
        if len(parts) != 4 or parts[2].upper().startswith("Z"):
            raise ProcessLookupError(pid)
        return ProcessIdentity(
            pid=pid,
            birth_id="ps-second:" + " ".join(parts[3].split()),
            process_group_id=int(parts[1]),
            parent_pid=int(parts[0]),
        )
    return _windows_process_identity(pid)


def process_is_alive(identity: ProcessIdentity) -> bool:
    try:
        current = process_identity(identity.pid)
    except (OSError, ProcessLookupError, ValueError):
        return False
    return current.birth_id == identity.birth_id


def owned_processes_are_quiescent(
    identities: Sequence[ProcessIdentity],
    *,
    owner_nonces: Sequence[str] = (),
) -> bool:
    """Return true only when durable process ownership proves no live process."""
    try:
        states = tuple(_identity_state(identity) for identity in identities)
        if any(state == "live" for state in states):
            return False
        if any(state == "stale" for state in states) and not _owner_nonce_scan_supported(
            owner_nonces
        ):
            return False
        return not _process_identities_with_owner_nonces(owner_nonces)
    except ProcessLedgerError:
        return False


def descendant_process_identities(
    root: ProcessIdentity,
) -> tuple[ProcessIdentity, ...]:
    """Snapshot the current birth-verified POSIX descendant closure."""
    if os.name != "posix" or not process_is_alive(root):
        return ()
    native = _native_descendant_process_identities(root.pid)
    if native is not None:
        return native
    descendants = _descendant_closure(root.pid, _posix_process_table())
    return tuple(descendants[pid] for pid in sorted(descendants))


def _native_descendant_process_identities(
    root_pid: int,
) -> tuple[ProcessIdentity, ...] | None:
    if sys.platform.startswith("linux"):
        child_reader = _linux_child_pids
    elif sys.platform == "darwin":
        child_reader = _darwin_child_pids
    else:
        return None
    descendants: dict[int, ProcessIdentity] = {}
    frontier = [root_pid]
    while frontier:
        parent_pid = frontier.pop()
        child_pids = child_reader(parent_pid)
        if child_pids is None:
            return None
        for child_pid in child_pids:
            if child_pid in descendants:
                continue
            try:
                identity = process_identity(child_pid)
            except (OSError, ProcessLookupError, ValueError):
                continue
            descendants[child_pid] = identity
            frontier.append(child_pid)
    return tuple(descendants[pid] for pid in sorted(descendants))


def _linux_child_pids(parent_pid: int) -> tuple[int, ...] | None:
    try:
        raw = Path(f"/proc/{parent_pid}/task/{parent_pid}/children").read_text(
            encoding="ascii"
        )
    except (FileNotFoundError, ProcessLookupError):
        return ()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return tuple(int(value) for value in raw.split())
    except ValueError:
        return None


def _darwin_child_pids(parent_pid: int) -> tuple[int, ...] | None:
    import ctypes

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_listchildpids = libproc.proc_listchildpids
        proc_listchildpids.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        proc_listchildpids.restype = ctypes.c_int
        required_bytes = proc_listchildpids(parent_pid, None, 0)
    except (AttributeError, OSError):
        return None
    if required_bytes <= 0:
        return ()
    count = max(1, required_bytes // ctypes.sizeof(ctypes.c_int))
    buffer = (ctypes.c_int * count)()
    found = proc_listchildpids(parent_pid, buffer, ctypes.sizeof(buffer))
    if found < 0:
        return None
    return tuple(int(buffer[index]) for index in range(min(found, count)))


def _linux_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    return _parse_linux_proc_stat(pid, raw)


def _parse_linux_proc_stat(pid: int, raw: str) -> ProcessIdentity | None:
    closing = raw.rfind(") ")
    if closing < 0:
        return None
    fields = raw[closing + 2:].split()
    if len(fields) < 20 or fields[0] == "Z":
        raise ProcessLookupError(pid)
    try:
        parent_pid = int(fields[1])
        process_group_id = int(fields[2])
        start_ticks = int(fields[19])
    except ValueError:
        return None
    return ProcessIdentity(
        pid=pid,
        birth_id=f"linux-proc:{start_ticks}",
        process_group_id=process_group_id,
        parent_pid=parent_pid,
    )


def _darwin_process_identity(pid: int) -> ProcessIdentity | None:
    """Use libproc microsecond start time; callers fall back to second-resolution ps."""
    import ctypes

    class ProcBSDInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        )
        proc_pidinfo.restype = ctypes.c_int
        info = ProcBSDInfo()
        size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (AttributeError, OSError):
        return None
    if size != ctypes.sizeof(info):
        if size <= 0:
            raise ProcessLookupError(pid)
        return None
    return ProcessIdentity(
        pid=pid,
        birth_id=f"darwin-proc:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
        process_group_id=int(info.pbi_pgid),
        parent_pid=int(info.pbi_ppid),
    )


def spawn_registered_process(
    ledger: ProcessLedger,
    command: Sequence[str],
    *,
    role: str,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: Any = subprocess.DEVNULL,
    stdout: Any = None,
    stderr: Any = None,
    start_new_session: bool = False,
    registration_hook: Any = None,
    release_hook: Any = None,
    handshake_timeout_s: float = 5.0,
    shim_command: Sequence[str] | None = None,
) -> subprocess.Popen[bytes]:
    """Launch through a shim that cannot exec the target before fsynced registration."""
    target = [str(part) for part in command]
    if not target:
        raise ValueError("registered subprocess command is empty")
    nonce = uuid4().hex
    with ledger.exclusive():
        ledger.begin_spawn_unlocked(role, nonce)
        process: subprocess.Popen[bytes] | None = None
        identity_read: int | None = None
        identity_write: int | None = None
        release_read: int | None = None
        release_write: int | None = None
        identity: ProcessIdentity | None = None
        try:
            identity_read, identity_write = os.pipe()
            release_read, release_write = os.pipe()
            if os.name == "posix":
                launch_command = list(shim_command) if shim_command is not None else [
                    sys.executable, "-m", "autodesign.process_supervision", "--spawn-shim",
                    "--identity-fd", str(identity_write), "--release-fd", str(release_read),
                    "--nonce", nonce,
                ]
                if start_new_session:
                    launch_command.append("--new-session")
                try:
                    process = subprocess.Popen(
                        launch_command,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        pass_fds=(identity_write, release_read),
                    )
                    ledger.record_shim_pid_unlocked(nonce, process.pid)
                finally:
                    os.close(identity_write)
                    identity_write = None
                    os.close(release_read)
                    release_read = None
                preliminary = process_identity(process.pid)
                ledger.record_shim_unlocked(nonce, preliminary)
                handshake_fd, identity_read = identity_read, None
                identity = _read_shim_identity(
                    handshake_fd, process.pid, timeout_s=handshake_timeout_s,
                )
            else:
                launch_command = list(shim_command) if shim_command is not None else [
                    sys.executable, "-m", "autodesign.process_supervision", "--spawn-shim",
                    "--identity-handle", str(_windows_os_handle(identity_write)),
                    "--release-handle", str(_windows_os_handle(release_read)),
                    "--nonce", nonce,
                ]
                if start_new_session:
                    launch_command.append("--new-session")
                try:
                    process = _windows_spawn_with_handles(
                        launch_command,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        inherited_fds=(identity_write, release_read),
                    )
                    ledger.record_shim_pid_unlocked(nonce, process.pid)
                finally:
                    os.close(identity_write)
                    identity_write = None
                    os.close(release_read)
                    release_read = None
                preliminary = process_identity(process.pid)
                ledger.record_shim_unlocked(nonce, preliminary)
                handshake_fd, identity_read = identity_read, None
                identity = _read_shim_identity(
                    handshake_fd, process.pid, timeout_s=handshake_timeout_s,
                )
            ledger.record_shim_unlocked(nonce, identity)
            if registration_hook is not None:
                registration_hook(identity)
            ledger.register_unlocked(identity, role=role, nonce=nonce)
            if release_hook is not None:
                release_hook(identity)
            target_env = dict(os.environ if env is None else env)
            target_env[_PROCESS_OWNER_ENV] = nonce
            payload = json.dumps({
                "command": target,
                "cwd": str(cwd) if cwd is not None else None,
                "env": target_env,
            }, separators=(",", ":")).encode("utf-8")
            _write_fd_with_timeout(
                release_write, _FRAME.pack(len(payload)) + payload + b"\x01",
                timeout_s=handshake_timeout_s,
            )
            os.close(release_write)
            release_write = None
            return process
        except BaseException as exc:
            for descriptor in (identity_read, identity_write, release_read, release_write):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if process is not None:
                _terminate_unreleased_process(process)
            failure = f"{type(exc).__name__}: {exc}"
            process_is_gone = process is None or process.poll() is not None
            identity_is_gone = identity is None or not process_is_alive(identity)
            if process_is_gone and identity_is_gone:
                ledger.rollback_spawn_unlocked(nonce, failure)
            else:
                ledger.fail_spawn_unlocked(nonce, failure)
            raise


def terminate_process_identities(
    identities: Sequence[ProcessIdentity],
    *,
    root_pid: int | None,
    grace_s: float,
    unresolved_spawns: Sequence[str] = (),
    owner_nonces: Sequence[str] = (),
) -> TerminationReport:
    registered = {identity.pid: identity for identity in identities}
    registered.update({
        identity.pid: identity
        for identity in _process_identities_with_owner_nonces(owner_nonces)
    })
    stale: dict[int, ProcessIdentity] = {}
    live: dict[int, ProcessIdentity] = {}
    for identity in registered.values():
        state = _identity_state(identity)
        if state == "live":
            live[identity.pid] = identity
        elif state == "stale":
            stale[identity.pid] = identity
    if os.name == "posix":
        process_table = _posix_process_table()
        for identity in tuple(live.values()):
            live.update(_descendant_closure(identity.pid, process_table))
    ordered = _deepest_first(tuple(live.values()))
    owned_groups = {
        identity.process_group_id
        for identity in live.values()
        if identity.process_group_id and identity.process_group_id > 1
    }
    if os.name == "posix":
        stale.update(_signal_many(ordered, signal.SIGTERM))
        stale.update(_signal_owned_groups(registered.values(), signal.SIGTERM))
    else:
        for identity in ordered:
            if _identity_state(identity) != "live":
                continue
            subprocess.run(
                ["taskkill", "/PID", str(identity.pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        if not any(process_is_alive(identity) for identity in ordered):
            break
        time.sleep(0.015)
    if os.name == "posix":
        live.update({
            identity.pid: identity
            for identity in _posix_process_table().values()
            if identity.process_group_id in owned_groups
        })
        ordered = _deepest_first(tuple(live.values()))
    survivors_before_kill = tuple(identity for identity in ordered if process_is_alive(identity))
    if os.name == "posix":
        stale.update(_signal_many(survivors_before_kill, signal.SIGKILL))
        stale.update(_signal_owned_groups(registered.values(), signal.SIGKILL))
    else:
        for identity in survivors_before_kill:
            if _identity_state(identity) != "live":
                continue
            subprocess.run(
                ["taskkill", "/PID", str(identity.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    hard_deadline = time.monotonic() + max(0.5, grace_s)
    while time.monotonic() < hard_deadline:
        if not any(process_is_alive(identity) for identity in ordered):
            break
        time.sleep(0.015)
    if os.name == "posix":
        escaped = tuple(
            identity for identity in _posix_process_table().values()
            if identity.process_group_id in owned_groups
        )
        _signal_many(escaped, signal.SIGKILL)
        verify_deadline = time.monotonic() + 0.5
        while escaped and time.monotonic() < verify_deadline:
            escaped = tuple(identity for identity in escaped if process_is_alive(identity))
            if escaped:
                time.sleep(0.015)
        for identity in escaped:
            live[identity.pid] = identity
    owner_survivors = _terminate_owner_processes_until_quiescent(
        owner_nonces,
        grace_s=grace_s,
    )
    for identity in owner_survivors:
        live[identity.pid] = identity
    survivors_by_pid = {
        identity.pid: identity
        for identity in live.values()
        if process_is_alive(identity)
    }
    stale_is_resolved = (
        bool(stale)
        and not unresolved_spawns
        and not survivors_by_pid
        and not owner_survivors
        and _owner_nonce_scan_supported(owner_nonces)
    )
    if not stale_is_resolved:
        survivors_by_pid.update(stale)
    survivors = tuple(survivors_by_pid.values())
    terminated = tuple(identity for identity in live.values() if not process_is_alive(identity))
    return TerminationReport(
        terminated=terminated,
        survivors=survivors,
        stale_identities=() if stale_is_resolved else tuple(stale.values()),
        unresolved_spawns=tuple(unresolved_spawns),
    )


def _terminate_owner_processes_until_quiescent(
    owner_nonces: Sequence[str],
    *,
    grace_s: float,
) -> tuple[ProcessIdentity, ...]:
    nonces = tuple(sorted({str(value) for value in owner_nonces if str(value)}))
    if not nonces or os.name != "posix":
        return ()
    deadline = time.monotonic() + max(0.5, grace_s)
    empty_scans = 0
    survivors: tuple[ProcessIdentity, ...] = ()
    while time.monotonic() < deadline:
        survivors = _process_identities_with_owner_nonces(nonces)
        if not survivors:
            empty_scans += 1
            if empty_scans >= 2:
                return ()
            time.sleep(0.015)
            continue
        empty_scans = 0
        _signal_many(survivors, signal.SIGKILL)
        time.sleep(0.015)
    return tuple(
        identity
        for identity in _process_identities_with_owner_nonces(nonces)
        if process_is_alive(identity)
    )


def _process_identities_with_owner_nonces(
    owner_nonces: Sequence[str],
) -> tuple[ProcessIdentity, ...]:
    nonces = {str(value) for value in owner_nonces if str(value)}
    if not nonces or os.name != "posix":
        return ()
    pids: set[int] = set()
    if sys.platform.startswith("linux"):
        markers = {
            f"{_PROCESS_OWNER_ENV}={nonce}".encode("utf-8")
            for nonce in nonces
        }
        try:
            proc_entries = tuple(Path("/proc").iterdir())
        except OSError as exc:
            raise ProcessLedgerError(
                f"owner process scan failed: cannot enumerate /proc: {exc}"
            ) from exc
        for entry in proc_entries:
            if not entry.name.isdigit():
                continue
            try:
                values = set((entry / "environ").read_bytes().split(b"\0"))
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                try:
                    owned_by_current_user = entry.stat().st_uid == os.geteuid()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError as stat_exc:
                    raise ProcessLedgerError(
                        f"owner process scan failed for pid {entry.name}: {stat_exc}"
                    ) from stat_exc
                if owned_by_current_user:
                    raise ProcessLedgerError(
                        f"owner process scan failed for pid {entry.name}: {exc}"
                    ) from exc
                continue
            except OSError as exc:
                raise ProcessLedgerError(
                    f"owner process scan failed for pid {entry.name}: {exc}"
                ) from exc
            if markers.intersection(values):
                pids.add(int(entry.name))
    elif sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["/bin/ps", "eww", "-axo", "pid=,command="],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise ProcessLedgerError(f"owner process scan failed: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            raise ProcessLedgerError(f"owner process scan failed: {detail}")
        markers = tuple(f"{_PROCESS_OWNER_ENV}={nonce}" for nonce in nonces)
        observed_pids: set[int] = set()
        for line in completed.stdout.splitlines():
            stripped = line.lstrip()
            raw_pid, separator, command = stripped.partition(" ")
            if not separator or not raw_pid.isdigit():
                continue
            observed_pids.add(int(raw_pid))
            if any(marker in command.split() for marker in markers):
                pids.add(int(raw_pid))
        if os.getpid() not in observed_pids:
            raise ProcessLedgerError(
                "owner process scan failed: ps output did not include the scanner process"
            )
    identities: list[ProcessIdentity] = []
    for pid in sorted(pids):
        try:
            identities.append(process_identity(pid))
        except (OSError, ProcessLookupError, ValueError):
            continue
    return tuple(identities)


def _owner_nonce_scan_supported(owner_nonces: Sequence[str]) -> bool:
    return (
        bool({str(value) for value in owner_nonces if str(value)})
        and os.name == "posix"
        and (sys.platform.startswith("linux") or sys.platform == "darwin")
    )


def _identity_state(identity: ProcessIdentity) -> str:
    try:
        current = process_identity(identity.pid)
    except (OSError, ProcessLookupError, ValueError):
        return "missing"
    return "live" if current.birth_id == identity.birth_id else "stale"


def _pid_exists_unverified(pid: int) -> bool:
    try:
        process_identity(pid)
    except (OSError, ProcessLookupError, ValueError):
        return False
    return True


def _signal_many(
    identities: Sequence[ProcessIdentity],
    sig: signal.Signals,
) -> dict[int, ProcessIdentity]:
    stale: dict[int, ProcessIdentity] = {}
    for identity in identities:
        state = _identity_state(identity)
        if state == "stale":
            stale[identity.pid] = identity
            continue
        if state != "live":
            continue
        try:
            os.kill(identity.pid, sig)
        except (ProcessLookupError, PermissionError):
            continue
    return stale


def _signal_owned_groups(
    registered: Iterable[ProcessIdentity],
    sig: signal.Signals,
) -> dict[int, ProcessIdentity]:
    stale: dict[int, ProcessIdentity] = {}
    signalled: set[int] = set()
    for identity in registered:
        group_id = identity.process_group_id
        if not group_id or group_id <= 1 or group_id in signalled:
            continue
        state = _identity_state(identity)
        if state == "stale":
            stale[identity.pid] = identity
            continue
        if state != "live":
            continue
        try:
            os.killpg(group_id, sig)
            signalled.add(group_id)
        except (ProcessLookupError, PermissionError):
            continue
    return stale


def _descendant_closure(root_pid: int, known: dict[int, ProcessIdentity]) -> dict[int, ProcessIdentity]:
    descendants: dict[int, ProcessIdentity] = {}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for identity in known.values():
            if identity.parent_pid == parent and identity.pid not in descendants:
                descendants[identity.pid] = identity
                frontier.append(identity.pid)
    return descendants


def _posix_process_table() -> dict[int, ProcessIdentity]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,state=,lstart="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    table: dict[int, ProcessIdentity] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5 or parts[3].upper().startswith("Z"):
            continue
        try:
            pid, ppid, pgid = (int(parts[index]) for index in range(3))
        except ValueError:
            continue
        try:
            table[pid] = process_identity(pid)
        except (OSError, ProcessLookupError, ValueError):
            continue
    return table


def _deepest_first(identities: Sequence[ProcessIdentity]) -> tuple[ProcessIdentity, ...]:
    by_pid = {identity.pid: identity for identity in identities}

    def depth(identity: ProcessIdentity) -> int:
        result = 0
        parent = identity.parent_pid
        seen: set[int] = set()
        while parent in by_pid and parent not in seen:
            seen.add(parent)
            result += 1
            parent = by_pid[parent].parent_pid
        return result

    return tuple(sorted(by_pid.values(), key=lambda item: (depth(item), item.pid), reverse=True))


def _read_shim_identity(
    descriptor: int,
    expected_pid: int,
    *,
    timeout_s: float,
) -> ProcessIdentity:
    try:
        line = _read_fd_line_with_timeout(descriptor, 4096, timeout_s=timeout_s)
    except (OSError, TimeoutError) as exc:
        raise ProcessLedgerError("spawn shim identity handshake failed") from exc
    try:
        identity = ProcessIdentity(**json.loads(line))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessLedgerError("spawn shim returned an invalid identity") from exc
    if identity.pid != expected_pid:
        raise ProcessLedgerError("spawn shim PID does not match launched process")
    return identity


def _read_fd_line_with_timeout(descriptor: int, limit: int, *, timeout_s: float) -> bytes:
    if os.name != "posix":
        completed: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

        def read_line() -> None:
            result = bytearray()
            try:
                while len(result) < limit:
                    chunk = os.read(descriptor, 1)
                    if not chunk:
                        break
                    result.extend(chunk)
                    if chunk == b"\n":
                        break
                completed.put(bytes(result))
            except BaseException as exc:
                completed.put(exc)

        threading.Thread(target=read_line, daemon=True).start()
        try:
            outcome = completed.get(timeout=max(0.01, timeout_s))
        except queue.Empty as exc:
            os.close(descriptor)
            raise TimeoutError("spawn shim identity handshake timed out") from exc
        os.close(descriptor)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    deadline = time.monotonic() + max(0.01, timeout_s)
    result = bytearray()
    try:
        while len(result) < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("spawn shim identity handshake timed out")
            if os.name == "posix":
                readable, _, _ = select.select([descriptor], [], [], remaining)
                if not readable:
                    raise TimeoutError("spawn shim identity handshake timed out")
            chunk = os.read(descriptor, 1)
            if not chunk:
                break
            result.extend(chunk)
            if chunk == b"\n":
                break
    finally:
        os.close(descriptor)
    return bytes(result)


def _write_fd_with_timeout(descriptor: int, payload: bytes, *, timeout_s: float) -> None:
    if os.name != "posix":
        completed: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def write_payload() -> None:
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise BrokenPipeError("spawn shim release pipe closed")
                    offset += written
                completed.put(None)
            except BaseException as exc:
                completed.put(exc)

        threading.Thread(target=write_payload, daemon=True).start()
        try:
            outcome = completed.get(timeout=max(0.01, timeout_s))
        except queue.Empty as exc:
            os.close(descriptor)
            raise TimeoutError("spawn shim release handshake timed out") from exc
        if outcome is not None:
            raise outcome
        return
    deadline = time.monotonic() + max(0.01, timeout_s)
    offset = 0
    while offset < len(payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("spawn shim release handshake timed out")
        if os.name == "posix":
            _, writable, _ = select.select([], [descriptor], [], remaining)
            if not writable:
                raise TimeoutError("spawn shim release handshake timed out")
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise BrokenPipeError("spawn shim release pipe closed")
        offset += written


def _windows_spawn_with_handles(
    command: Sequence[str],
    *,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    inherited_fds: Sequence[int],
) -> subprocess.Popen[bytes]:
    """Pass only the two handshake handles; the target remains in the root Job."""
    import msvcrt

    handles = [msvcrt.get_osfhandle(descriptor) for descriptor in inherited_fds]
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": handles}
    for descriptor in inherited_fds:
        os.set_inheritable(descriptor, True)
    try:
        return subprocess.Popen(
            command, stdin=stdin, stdout=stdout, stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True, startupinfo=startup,
        )
    finally:
        for descriptor in inherited_fds:
            os.set_inheritable(descriptor, False)


def _windows_os_handle(descriptor: int) -> int:
    import msvcrt
    return int(msvcrt.get_osfhandle(descriptor))


def _terminate_unreleased_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.kill(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except Exception:
                pass


def _shim_main(args: argparse.Namespace) -> int:
    if args.new_session and os.name == "posix":
        os.setsid()
    identity = process_identity(os.getpid())
    encoded_identity = (json.dumps(asdict(identity), separators=(",", ":")) + "\n").encode()
    identity_fd = args.identity_fd
    release_fd = args.release_fd
    if os.name == "nt":
        import msvcrt
        if args.identity_handle is not None:
            identity_fd = msvcrt.open_osfhandle(args.identity_handle, os.O_WRONLY)
        if args.release_handle is not None:
            release_fd = msvcrt.open_osfhandle(args.release_handle, os.O_RDONLY)
    if identity_fd is not None:
        try:
            os.write(identity_fd, encoded_identity)
        except BrokenPipeError:
            return 76
        finally:
            os.close(identity_fd)
    else:
        sys.stdout.buffer.write(encoded_identity)
        sys.stdout.buffer.flush()
    if release_fd is None:
        return 71
    with os.fdopen(release_fd, "rb", closefd=True) as release:
        try:
            header = _read_exact(release, _FRAME.size)
        except EOFError:
            return 76
        (length,) = _FRAME.unpack(header)
        if length <= 0 or length > 4 * 1024 * 1024:
            return 72
        try:
            payload = json.loads(_read_exact(release, length))
            release_marker = _read_exact(release, 1)
        except (EOFError, json.JSONDecodeError):
            return 77
        if release_marker != b"\x01":
            return 73
    command = payload.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        return 74
    cwd = payload.get("cwd")
    env = payload.get("env")
    if cwd is not None:
        os.chdir(cwd)
    target_env = os.environ if env is None else {str(key): str(value) for key, value in env.items()}
    os.execvpe(command[0], command, target_env)
    return 75


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = stream.read(count - len(result))
        if not chunk:
            raise EOFError("private spawn handshake closed before release")
        result.extend(chunk)
    return bytes(result)


def _lock_file(handle: BinaryIO, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + max(0.01, timeout_s)
    if os.name == "posix":
        import fcntl
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ProcessLedgerError("timed out acquiring process ledger lock")
                time.sleep(0.01)
    else:
        import msvcrt
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProcessLedgerError("timed out acquiring process ledger lock")
                time.sleep(0.01)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "posix":
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _windows_process_identity(pid: int) -> ProcessIdentity:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_windows_api(kernel32)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise ProcessLookupError(pid)
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise ProcessLookupError(pid)
        birth = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ProcessIdentity(pid, str(birth), None, None)
    finally:
        kernel32.CloseHandle(handle)


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spawn-shim", action="store_true")
    parser.add_argument("--identity-fd", type=int)
    parser.add_argument("--release-fd", type=int)
    parser.add_argument("--identity-handle", type=int)
    parser.add_argument("--release-handle", type=int)
    parser.add_argument("--nonce")
    parser.add_argument("--new-session", action="store_true")
    args = parser.parse_args()
    if not args.spawn_shim:
        parser.error("--spawn-shim is required")
    return _shim_main(args)


if __name__ == "__main__":
    raise SystemExit(_main())

"""A Windows job object that ends every game a run started, even if the runner itself dies.

Every peer process is assigned to one job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: when the job's
last handle closes (the runner exits, normally or not), Windows ends every process still in it. So
no hidden server or scripted client outlives a test run. On other systems the job does nothing and
the runner ends its processes itself.

Standard library only (ctypes).
"""
from __future__ import annotations

import os
import subprocess

WINDOWS = os.name == "nt"

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicAccountingInformation = 1
    JobObjectExtendedLimitInformation = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
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

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BASIC_ACCOUNTING(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


class Job:
    """One job per test run. Use as a context manager; closing it ends every process left in it."""

    def __init__(self) -> None:
        self.handle = None
        if not WINDOWS:
            return
        h = _k32.CreateJobObjectW(None, None)
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            _k32.CloseHandle(h)
            raise OSError(err, "SetInformationJobObject failed")
        self.handle = h

    def add(self, proc: subprocess.Popen) -> None:
        if self.handle is None:
            return
        if not _k32.AssignProcessToJobObject(self.handle, int(proc._handle)):  # noqa: SLF001
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def active(self) -> int:
        """How many processes of the job are still running."""
        if self.handle is None:
            return 0
        info = _BASIC_ACCOUNTING()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicAccountingInformation,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return int(info.ActiveProcesses)

    def terminate(self, code: int = 1) -> None:
        if self.handle is not None:
            _k32.TerminateJobObject(self.handle, code)

    def close(self) -> None:
        if self.handle is not None:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "Job":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

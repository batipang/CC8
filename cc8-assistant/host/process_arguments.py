"""Read OS argument boundaries for an already identified process."""

import os
from pathlib import Path
import sys


def mac_process_arguments(pid):
    import ctypes

    # Darwin returns argc, a NUL-terminated executable path, padding and argv.
    # See Apple's adv_cmds ps/print.c (KERN_PROCARGS2).
    arg_max = os.sysconf('SC_ARG_MAX')
    if not 0 < arg_max <= 2 * 1024**2:
        return []
    sysctl = ctypes.CDLL(None).sysctl
    sysctl.argtypes = (
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(arg_max)
    buf = ctypes.create_string_buffer(size.value)
    if sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    end = size.value
    header = ctypes.sizeof(ctypes.c_int)
    if not header < end <= len(buf):
        return []
    argc = ctypes.c_int.from_buffer(buf).value
    if not 0 < argc <= 128:
        return []
    raw = buf.raw
    start = raw.find(b'\0', header, end)
    if start <= header:
        return []
    while start < end and raw[start] == 0:
        start += 1
    args = []
    for _ in range(argc):
        stop = raw.find(b'\0', start, end)
        if stop == -1:
            return []
        args.append(os.fsdecode(raw[start:stop]))
        start = stop + 1
    return args


def process_arguments(pid):
    if type(pid) is not int or not 0 < pid < 2**31:
        return []
    try:
        if sys.platform == 'darwin':
            return mac_process_arguments(pid)
        if sys.platform.startswith('linux'):
            with (Path('/proc') / str(pid) / 'cmdline').open('rb') as stream:
                raw = stream.read(2 * 1024**2 + 1)
            if len(raw) <= 2 * 1024**2 and raw.endswith(b'\0'):
                args = raw[:-1].split(b'\0', 128)
                if len(args) <= 128:
                    return [os.fsdecode(arg) for arg in args]
    except (OSError, ValueError):
        pass
    return []

"""Bounded owner-process gauges; no process arguments, file names or device reads."""

import math
import os
import platform
import resource
import subprocess
import threading
import time


def positive(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum


class ResourceSampler:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.last = None
        self.cached = None

    def sample(self):
        with self.lock:
            now = self.clock()
            if self.last is not None and 0 <= now - self.last < 1:
                return dict(self.cached)
            current_rss = None
            try:
                output = subprocess.check_output(
                    ['/bin/ps', '-p', str(os.getpid()), '-o', 'rss='],
                    text=True,
                    timeout=0.5,
                    stderr=subprocess.DEVNULL,
                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                ).strip()
                if output.isascii() and output.isdigit() and len(output) <= 16:
                    value = int(output) * 1024
                    current_rss = value if positive(value, 2**50) else None
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            peak = cpu = None
            try:
                usage = resource.getrusage(resource.RUSAGE_SELF)
                peak = usage.ru_maxrss * (1 if platform.system() == 'Darwin' else 1024)
                cpu = usage.ru_utime + usage.ru_stime
            except (OSError, ValueError):
                pass
            descriptors = None
            try:
                # scandir's own descriptor may be included; never export names.
                with os.scandir(
                    '/dev/fd' if platform.system() == 'Darwin' else '/proc/self/fd'
                ) as entries:
                    descriptors = sum(1 for _ in entries)
            except OSError:
                pass
            self.cached = {
                'rss_bytes': current_rss,
                'peak_rss_bytes': int(peak) if positive(peak, 2**50) else None,
                'cpu_seconds': round(cpu, 6) if positive(cpu, 10**12) else None,
                'python_threads': threading.active_count(),
                'open_fds': descriptors,
                'fd_probe_may_be_included': True,
            }
            self.last = now
            return dict(self.cached)

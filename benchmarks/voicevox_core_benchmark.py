#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright==1.63.0", "platformdirs==4.12.3", "cmake==4.4.4", "ninja==1.13.2", "libclang==18.1.1", "psutil==7.2.2"]
# ///
"""Reproducible VOICEVOX CORE CPU/browser benchmark (one-file distribution).

Run: uv run benchmark.py [--threads 4]
Optimization experiment: uv run benchmark.py --experiments --threads 2

The first run downloads pinned Rust/Emscripten, models, runtimes and Chromium,
then builds CORE; allow several GB and several minutes. A native C/C++ compiler
is required (Windows: MSVC Native Tools prompt; macOS: Xcode Command Line Tools;
Linux: cc and c++). Linux also needs Chromium's system libraries; if missing,
run: uv run --with playwright==1.63.0 python -m playwright install-deps chromium
That system-package step may require administrator permission. Later runs reuse
checked downloads and builds.

Outputs: standalone HTML with CPU time-series/charts/collapsed CSV, plus JSON.
No text analyzer or dictionary is used. No benchmark measurements are simulated.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

CORE_COMMIT = "9b539761f3e152b966e08c2de0784129fe8cf68d"
ORT_BUILDER_COMMIT = "117593885cd2a66e9cf17b4059e6424d7ea528c9"
ORT_VERSION = "1.23.2"
VOCODER_MODEL_SHA256 = "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820"
TRIALS_PER_BLOCK = 5
BLOCKS_PER_MODE = 3
SCHEMA_VERSION = 4


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    threads: int
    backend: str
    core_optimization: str = "z"
    graph_optimization: int = 1
    experimental: bool = False
    fixed_shape: bool = False
    spin_off: bool = False
    execution_provider: str = "CPU"
    revectorize: bool = False


@dataclass(frozen=True)
class Trial:
    order: int
    mode: str
    block: int
    trial: int
    threads: int
    elapsed_s: float
    audio_s: float
    rtf: float
    cpu_time_s: float
    cpu_window_s: float
    cpu_avg_cores: float
    cpu_percent: float
    cpu_samples: int
    cpu_processes: int
    cpu_incomplete: bool
    cpu_trace: tuple[CpuInterval, ...]


@dataclass(frozen=True)
class Block:
    mode: str
    number: int


CPU_SAMPLE_INTERVAL_S = 0.1
CPU_PADDING_S = 1.0
PROGRESS_INTERVAL_S = 15.0


def progress(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class CpuInterval:
    start_s: float
    end_s: float
    cpu_time_s: float
    cpu_percent: float
    cpu_processes: int
    cpu_incomplete: bool
    phase: str = "action"


@dataclass(frozen=True)
class CpuMeasurement:
    cpu_time_s: float
    cpu_window_s: float
    cpu_avg_cores: float
    cpu_percent: float
    cpu_samples: int
    cpu_processes: int
    cpu_incomplete: bool
    cpu_trace: tuple[CpuInterval, ...]


class ProcessCpuSampler:
    """Sum user+system CPU for one explicitly selected process tree.

    The CPU window surrounds the request/response and counter reads, separately
    from the existing internal synthesis timer. CPU times include all threads.
    Never add children_user/system: descendant processes are counted directly.
    Lost/late processes mark CPU data incomplete; those rows remain in CSV but
    are excluded from the report's CPU average. Polling cannot observe a child
    that starts and exits entirely between samples.
    """
    def __init__(self, root_pid: int, label: str, interval: float = CPU_SAMPLE_INTERVAL_S):
        import psutil
        import threading
        if root_pid == os.getpid():
            raise ValueError("Refusing to include the Python orchestrator in target CPU")
        if interval <= 0 or not math.isfinite(interval):
            raise ValueError("CPU sample interval must be positive")
        self.psutil = psutil
        self.root = psutil.Process(root_pid)
        self.root_identity = (root_pid, self.root.create_time())
        self.label, self.interval = label, interval
        self.stop = threading.Event()
        self.sample_lock = threading.Lock()
        self.phase = "pre"
        self.phase_started_wall = 0.0
        self.processes: dict[tuple[int, float], Any] = {}
        self.previous: dict[tuple[int, float], float] = {}
        self.lost: set[tuple[int, float]] = set()
        self.total = 0.0
        self.samples = 0
        self.incomplete = False
        self.error: Exception | None = None
        self.started_epoch = 0.0
        self.started_wall = 0.0
        self.baseline_wall = 0.0
        self.previous_sample_wall = 0.0
        self.intervals: list[CpuInterval] = []

    def _sample(self, initial: bool = False) -> None:
        try:
            before = self.total
            if not self.root.is_running():
                raise RuntimeError("Target CPU root exited or its PID was reused")
            candidates = [self.root] + self.root.children(recursive=True)
            for process in candidates:
                try:
                    identity = (process.pid, process.create_time())
                    self.processes.setdefault(identity, process)
                except self.psutil.NoSuchProcess:
                    self.incomplete = True
            for identity, process in list(self.processes.items()):
                if identity in self.lost:
                    continue
                try:
                    # cpu_times alone does not guard against PID reuse.
                    if not process.is_running():
                        raise self.psutil.NoSuchProcess(process.pid)
                    times = process.cpu_times()
                    value = float(times.user + times.system)
                    if not math.isfinite(value) or value < 0:
                        raise RuntimeError("Invalid process CPU counter")
                    if identity not in self.previous:
                        if initial:
                            self.previous[identity] = value
                            continue
                        phase_epoch = self.started_epoch + self.phase_started_wall - self.started_wall
                        if identity[1] < phase_epoch:
                            # Never move a newly discovered child's earlier-phase
                            # lifetime CPU into this phase's counters. Its late
                            # discovery also invalidates earlier coverage where
                            # it could already have consumed unobserved CPU.
                            birth_wall = self.started_wall + identity[1] - self.started_epoch
                            affected = {point.phase for point in self.intervals if point.end_s > birth_wall}
                            self.intervals = [replace(point, cpu_incomplete=True) if point.phase in affected else point
                                              for point in self.intervals]
                            self.previous[identity] = value
                            self.incomplete = True
                            continue
                        previous_epoch = self.started_epoch + self.previous_sample_wall - self.started_wall
                        if identity[1] < previous_epoch:
                            # Lifetime CPU is attributable to this trial, but a
                            # late discovery cannot place it in the right interval.
                            # Birth-time rounding is treated conservatively too.
                            self.incomplete = True
                        self.previous[identity] = 0.0
                    delta = value - self.previous[identity]
                    if delta < -1e-6:
                        raise RuntimeError("Process CPU counter moved backwards")
                    self.total += max(0.0, delta)
                    self.previous[identity] = value
                except self.psutil.NoSuchProcess:
                    if identity == self.root_identity:
                        raise RuntimeError("Target CPU root disappeared before the final sample")
                    self.lost.add(identity)
                    self.incomplete = True
            sampled = time.perf_counter()
            if initial:
                self.baseline_wall = sampled
            else:
                span = sampled - self.previous_sample_wall
                if span <= 0:
                    raise RuntimeError("CPU snapshot clock did not advance")
                delta = self.total - before
                self.intervals.append(CpuInterval(self.previous_sample_wall, sampled, delta,
                                                  100 * delta / span, len(self.processes), self.incomplete, self.phase))
            self.previous_sample_wall = sampled
            self.samples += 1
        except Exception as error:
            self.error = error
            self.stop.set()

    def measure(self, action: Callable[[], tuple[float, float]]) -> tuple[float, float, CpuMeasurement]:
        import threading
        self.started_wall = time.perf_counter()
        self.started_epoch = time.time()
        with self.sample_lock:
            self._sample(initial=True)
            self.phase_started_wall = self.previous_sample_wall
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        def sample_loop() -> None:
            last_heartbeat = time.perf_counter()
            while not self.stop.wait(self.interval):
                with self.sample_lock:
                    self._sample()
                now = time.perf_counter()
                if now - last_heartbeat >= 30:
                    progress(f"{self.label}: synthesis still running ({now - self.started_wall:.0f}s)")
                    last_heartbeat = now
        worker = threading.Thread(target=sample_loop, name="benchmark-cpu-sampler", daemon=True)
        worker.start()
        try:
            if self.stop.wait(CPU_PADDING_S):
                raise RuntimeError(f"CPU pre-observation failed for {self.label}: {self.error}") from self.error
            with self.sample_lock:
                self._sample()
                action_cpu_start, action_wall_start = self.total, self.previous_sample_wall
                self.phase = "action"
                self.phase_started_wall = action_wall_start
                self.incomplete = False
            if self.error:
                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
            request_started = time.perf_counter()
            elapsed, duration = action()
            with self.sample_lock:
                self._sample()
                action_cpu_end, action_wall_end = self.total, self.previous_sample_wall
                self.phase = "post"
                self.phase_started_wall = action_wall_end
                self.incomplete = False
            if self.error:
                raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
            self.stop.wait(CPU_PADDING_S)
        finally:
            self.stop.set()
            worker.join(timeout=5)
            if worker.is_alive():
                self.error = RuntimeError("CPU sampler did not stop")
            elif self.error is None:
                with self.sample_lock:
                    self._sample()
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        total, window = action_cpu_end - action_cpu_start, action_wall_end - action_wall_start
        cores = total / window
        trace = tuple(CpuInterval(item.start_s - request_started, item.end_s - request_started,
                                  item.cpu_time_s, item.cpu_percent, item.cpu_processes, item.cpu_incomplete, item.phase)
                      for item in self.intervals)
        action_incomplete = any(item.cpu_incomplete for item in trace if item.phase == "action")
        return elapsed, duration, CpuMeasurement(total, window, cores, 100 * cores,
                                                self.samples, len(self.processes), action_incomplete, trace)


def chromium_process_id(browser: Any) -> int:
    """Use the public browser-level CDP API, never Playwright internals."""
    session = browser.new_browser_cdp_session()
    try:
        response = session.send("SystemInfo.getProcessInfo")
    except Exception as error:
        raise RuntimeError("This Chromium does not provide browser process IDs for CPU sampling; use the bundled Chromium") from error
    finally:
        session.detach()
    roots = [int(process["id"]) for process in response["processInfo"] if process.get("type") == "browser"]
    if len(roots) != 1 or roots[0] <= 0 or roots[0] == os.getpid():
        raise RuntimeError("Could not identify the benchmark Chromium process safely")
    return roots[0]


def weighted_cpu_percent(trials: list[dict[str, Any]]) -> float | None:
    complete = [row for row in trials if not row["cpu_incomplete"]]
    if not complete:
        return None
    return 100 * sum(row["cpu_time_s"] for row in complete) / sum(row["cpu_window_s"] for row in complete)


def make_schedule(modes: list[Mode], seed: int, *, balanced: bool = False) -> list[Block]:
    """Three shuffled rounds, each containing one five-trial block per mode."""
    if not modes or len({mode.key for mode in modes}) != len(modes):
        raise ValueError("Mode keys must be nonempty and unique")
    rng = random.Random(seed)
    if balanced:
        if len(modes) not in (2, 3, 4, 5, 6, 7) or BLOCKS_PER_MODE != 3:
            raise ValueError("Balanced experiments require two to seven modes and three rounds")
        base = list(modes)
        rng.shuffle(base)
        if len(modes) == 7:
            indices = ((0, 1, 2, 3, 4, 5, 6), (3, 5, 4, 6, 2, 1, 0), (6, 4, 5, 2, 0, 3, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 6:
            indices = ((0, 1, 2, 3, 4, 5), (5, 4, 3, 2, 1, 0), (2, 3, 4, 5, 0, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 5:
            indices = ((0, 1, 2, 3, 4), (4, 3, 0, 2, 1), (2, 4, 1, 0, 3))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 4:
            indices = ((0, 1, 2, 3), (1, 0, 3, 2), (2, 3, 0, 1))
            rotations = [[base[index] for index in row] for row in indices]
        elif len(modes) == 3:
            rotations = [base[i:] + base[:i] for i in range(3)]
        else:
            rotations = [base[:], base[::-1], base[::rng.choice((1, -1))]]
        rng.shuffle(rotations)
        return [Block(mode.key, number) for number, row in enumerate(rotations, 1) for mode in row]
    schedule = []
    for block_number in range(1, BLOCKS_PER_MODE + 1):
        round_modes = list(modes)
        rng.shuffle(round_modes)
        schedule.extend(Block(mode.key, block_number) for mode in round_modes)
    return schedule


def logical_cpu_count() -> int:
    # Respect the process's available logical CPUs in containers/affinity masks.
    process_count = getattr(os, "process_cpu_count", lambda: None)()
    if process_count:
        return process_count
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def default_threads() -> int:
    return max(1, logical_cpu_count() // 2)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cached_download(url: str, root: Path, filename: str, *,
                    expected_sha256: str | None = None) -> Path:
    """A partial download is never reused. Every cache hit rechecks its digest.

    An upstream SHA-256 is checked when supplied. Otherwise the manifest records
    the digest seen on the first successful HTTPS download (integrity on reuse,
    not a claim of independent supply-chain authentication).
    """
    if not url.startswith("https://"):
        raise ValueError("Downloads require HTTPS")
    if Path(filename).name != filename:
        raise ValueError("Download filename must not contain a directory")
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    folder = root / "downloads" / key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    receipt = folder / "complete.json"
    if target.is_file() and receipt.is_file():
        try:
            saved = json.loads(receipt.read_text("utf-8"))
            expected = expected_sha256 or saved["sha256"]
            if saved["url"] == url and target.stat().st_size == saved["bytes"] and sha256_file(target) == expected:
                progress(f"Cache hit: {filename}")
                return target
        except (OSError, ValueError, KeyError):
            pass
    descriptor, name = tempfile.mkstemp(prefix=".download-", dir=folder)
    partial = Path(name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "voicevox-core-benchmark/1"})
        digest = hashlib.sha256()
        total = 0
        progress(f"Downloading {filename}")
        last_progress = time.monotonic()
        with os.fdopen(descriptor, "wb") as out, urllib.request.urlopen(request, timeout=120) as response:
            if not response.geturl().startswith("https://"):
                raise RuntimeError("Download redirected away from HTTPS")
            length = response.headers.get("Content-Length")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                out.write(chunk)
                digest.update(chunk)
                total += len(chunk)
                now = time.monotonic()
                if now - last_progress >= 5:
                    size = f" / {int(length) / 2**20:.1f} MiB" if length else " MiB"
                    progress(f"{filename}: {total / 2**20:.1f}{size} downloaded")
                    last_progress = now
            if length is not None and total != int(length):
                raise RuntimeError(f"Incomplete download: {filename}")
            out.flush()
            os.fsync(out.fileno())
        actual = digest.hexdigest()
        progress(f"Downloaded {filename}: {total / 2**20:.1f} MiB; verifying checksum")
        if expected_sha256 and actual != expected_sha256:
            raise RuntimeError(f"SHA-256 mismatch: {filename}")
        os.replace(partial, target)
        atomic_write(receipt, json.dumps({"url": url, "sha256": actual, "bytes": total}).encode())
        return target
    finally:
        partial.unlink(missing_ok=True)


def cache_root() -> Path:
    try:
        from platformdirs import user_cache_path
    except ImportError:
        # Keep report-only and self-test usable with the Python standard library.
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library/Caches"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return base / "voicevox-core-benchmark"
    return user_cache_path("voicevox-core-benchmark", appauthor=False)


def checked_run(command: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None) -> str:
    """Capture output for errors/return values while keeping long work visible."""
    import queue
    import threading
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    messages: queue.Queue[str | None] = queue.Queue()
    def read_output() -> None:
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    output: list[str] = []
    last_line = ""
    last_report = started
    label = Path(command[0]).name
    if len(command) > 1:
        label += " " + Path(command[1]).name
    try:
        finished = False
        while not finished:
            try:
                line = messages.get(timeout=1.0)
                if line is None:
                    finished = True
                else:
                    output.append(line)
                    if line.strip():
                        last_line = line.strip()[-180:]
            except queue.Empty:
                pass
            now = time.monotonic()
            if not finished and now - last_report >= PROGRESS_INTERVAL_S:
                progress(f"{label}: running {now - started:.0f}s" + (f"; {last_line}" if last_line else ""))
                last_report = now
        code = process.wait()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        reader.join(timeout=5)
        process.stdout.close()
    text_output = "".join(output)
    duration = time.monotonic() - started
    if code:
        raise RuntimeError(f"Command failed ({code}): {' '.join(command)}\n{text_output[-10000:]}")
    if duration >= 5:
        progress(f"{label}: completed in {duration:.1f}s")
    return text_output.strip()

def command_version(command: list[str]) -> str | None:
    try:
        return checked_run(command).splitlines()[0]
    except (OSError, RuntimeError, IndexError):
        return None


def cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if sys.platform == "darwin":
            return checked_run(["sysctl", "-n", "machdep.cpu.brand_string"])
    except (OSError, RuntimeError):
        pass
    return platform.processor() or platform.machine() or "unknown"


def environment_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "cpu": cpu_model(),
        "available_logical_cpus": logical_cpu_count(),
        "host_logical_cpus": os.cpu_count() or "unknown",
        "python": platform.python_version(),
        "core_commit": CORE_COMMIT,
        "onnxruntime_version": ORT_VERSION,
        "onnxruntime_builder_commit": ORT_BUILDER_COMMIT,
    }
    if sys.platform == "darwin":
        info["os"] = f"macOS {platform.mac_ver()[0]} (Darwin {platform.release()})"
    elif sys.platform.startswith("linux"):
        try:
            info["os"] = f"{platform.freedesktop_os_release().get('PRETTY_NAME', 'Linux')} (kernel {platform.release()})"
        except OSError:
            pass
    for key, command in (("uv", ["uv", "--version"]), ("rust", ["rustc", "--version"]),
                         ("emscripten", ["emcc", "--version"])):
        if version := command_version(command):
            info[key] = version
    # CPU affinity / cgroup limits explain cloud results without exposing hostnames.
    if hasattr(os, "sched_getaffinity"):
        info["affinity_logical_cpus"] = len(os.sched_getaffinity(0))
    limit_file = Path("/sys/fs/cgroup/cpu.max")
    if sys.platform.startswith("linux") and limit_file.exists():
        try:
            quota, period = limit_file.read_text().strip().split()
            if quota != "max":
                info["cgroup_cpu_quota"] = int(quota) / int(period)
        except (OSError, ValueError, ZeroDivisionError):
            pass
    if hasattr(os, "sysconf"):
        try:
            info["memory_gib"] = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 2)
        except (ValueError, OSError):
            pass
    elif sys.platform == "win32":
        import ctypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ("total_phys", "avail_phys", "total_page", "avail_page", "total_virtual", "avail_virtual", "avail_extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            info["memory_gib"] = round(status.total_phys / 2**30, 2)
    return info


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if wav.getnframes() < 1 or wav.getframerate() < 1:
            raise ValueError("Generated WAV is empty")
        return wav.getnframes() / wav.getframerate()


def waveform_comparison(reference_wav: bytes, reference_raw: bytes, wav: bytes, raw: bytes) -> dict[str, Any]:
    """Untimed exactness check, with error magnitudes but no perceptual tolerance."""
    import array
    def pcm(data: bytes) -> tuple[tuple[int, ...], bytes]:
        with wave.open(io.BytesIO(data), "rb") as stream:
            shape = (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes())
            if shape[1] != 2 or shape[3] < 1:
                raise RuntimeError("Expected nonempty signed PCM16 output")
            payload = stream.readframes(shape[3])
            if len(payload) != shape[0] * shape[1] * shape[3]:
                raise RuntimeError("Truncated PCM waveform")
            return shape, payload
    reference_shape, reference_pcm = pcm(reference_wav)
    shape, values_pcm = pcm(wav)
    if shape != reference_shape or len(raw) != len(reference_raw) or not raw or len(raw) % 4:
        raise RuntimeError("Output shape/dtype differs from the same-run reference")
    def values(data: bytes, typecode: str) -> Any:
        result = array.array(typecode)
        result.frombytes(data)
        if sys.byteorder != "little":
            result.byteswap()
        return result
    floats, ref_floats = values(raw, "f"), values(reference_raw, "f")
    if not all(math.isfinite(value) for value in floats) or not all(math.isfinite(value) for value in ref_floats):
        raise RuntimeError("FP32 output contains NaN or infinity")
    float_errors = [float(a) - float(b) for a, b in zip(floats, ref_floats)]
    pcm_errors = [int(a) - int(b) for a, b in zip(values(values_pcm, "h"), values(reference_pcm, "h"))]
    rmse = math.sqrt(math.fsum(error * error for error in float_errors) / len(float_errors))
    reference_rms = math.sqrt(math.fsum(float(value) ** 2 for value in ref_floats) / len(ref_floats))
    return {"wav_format": {"channels": shape[0], "sample_bytes": shape[1], "sample_rate": shape[2], "frames": shape[3]},
            "fp32_samples": len(floats), "finite": True, "pcm_exact": values_pcm == reference_pcm,
            "pcm_sha256": hashlib.sha256(values_pcm).hexdigest(), "pcm_changed_samples": sum(error != 0 for error in pcm_errors),
            "pcm_max_abs_lsb": max(map(abs, pcm_errors)), "pcm_rmse_lsb": math.sqrt(math.fsum(error * error for error in pcm_errors) / len(pcm_errors)),
            "fp32_exact": raw == reference_raw, "fp32_sha256": hashlib.sha256(raw).hexdigest(),
            "fp32_changed_samples": sum(a != b for a, b in zip(values(raw, "I"), values(reference_raw, "I"))),
            "fp32_max_abs": max(map(abs, float_errors)), "fp32_rmse": rmse,
            "fp32_relative_rmse": rmse / max(reference_rms, 1e-12), "relative_rms_floor": 1e-12}


def verify_outputs(runners: dict[str, Any], reference: str, log: list[dict[str, Any]]) -> dict[str, Any]:
    progress(f"Checking PCM and pre-PCM FP32 outputs against {reference} (untimed)")
    reference_wav = runners[reference].wav.read_bytes()
    reference_raw = runners[reference].raw_wave()
    runners[reference].synthesize(save=True)
    repeated = waveform_comparison(reference_wav, reference_raw, runners[reference].wav.read_bytes(), runners[reference].raw_wave())
    comparisons = {}
    raw_outputs = {reference: reference_raw}
    wav_outputs = {key: runner.wav.read_bytes() for key, runner in runners.items()}
    for key, runner in runners.items():
        raw = reference_raw if key == reference else runner.raw_wave()
        raw_outputs[key] = raw
        comparisons[key] = waveform_comparison(reference_wav, reference_raw, runner.wav.read_bytes(), raw)
        progress(f"Output {key}: PCM {'exact' if comparisons[key]['pcm_exact'] else 'DIFFERS'}, FP32 {'exact' if comparisons[key]['fp32_exact'] else 'DIFFERS'}")
    result = {"reference_mode": reference, "reference_deterministic": repeated["pcm_exact"] and repeated["fp32_exact"],
              "reference_repeat": repeated, "modes": comparisons}
    if "browser_xnnpack" in runners:
        fixed = "browser_mt_fixed"
        runners[fixed].synthesize(save=True)
        repeat_fixed = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], runners[fixed].wav.read_bytes(), runners[fixed].raw_wave())
        result["fixed_control_repeat"] = repeat_fixed
        result["reference_deterministic"] = result["reference_deterministic"] and repeat_fixed["pcm_exact"] and repeat_fixed["fp32_exact"]
        result["xnnpack_vs_fixed_control"] = waveform_comparison(wav_outputs[fixed], raw_outputs[fixed], wav_outputs["browser_xnnpack"], raw_outputs["browser_xnnpack"])
    if "browser_xnnpack_revectorize" in runners:
        xnn, combined = "browser_xnnpack", "browser_xnnpack_revectorize"
        runners[xnn].synthesize(save=True)
        repeat_xnn = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], runners[xnn].wav.read_bytes(), runners[xnn].raw_wave())
        result["xnnpack_reference_repeat"] = repeat_xnn
        result["reference_deterministic"] = result["reference_deterministic"] and repeat_xnn["pcm_exact"] and repeat_xnn["fp32_exact"]
        result["combined_vs_xnnpack"] = waveform_comparison(wav_outputs[xnn], raw_outputs[xnn], wav_outputs[combined], raw_outputs[combined])
        result["combined_vs_fixed_control"] = waveform_comparison(wav_outputs["browser_mt_fixed"], raw_outputs["browser_mt_fixed"], wav_outputs[combined], raw_outputs[combined])
    log.append({"event": "untimed_output_checks", "reference": reference, "reference_deterministic": result["reference_deterministic"]})
    return result


def trial_csv(trials: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=[key for key in Trial.__dataclass_fields__ if key != "cpu_trace"], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trials)
    return stream.getvalue()


def cpu_trace_csv(trials: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    context = ("order", "mode", "block", "trial", "threads")
    writer = csv.DictWriter(stream, fieldnames=[*context, "interval", *CpuInterval.__dataclass_fields__])
    writer.writeheader()
    for trial in trials:
        for index, point in enumerate(trial.get("cpu_trace", ()), 1):
            writer.writerow({**{key: trial[key] for key in context}, "interval": index, **point, "phase": point.get("phase", "action")})
    return stream.getvalue()


def validate_results(result: dict[str, Any]) -> None:
    if result.get("schema_version") not in (2, 3, SCHEMA_VERSION):
        raise ValueError("Unsupported result schema")
    for field in ("style_id", "seed"):
        if type(result[field]) is not int:
            raise ValueError(f"Invalid {field}")
    if not isinstance(result["audio_s"], (float, int)) or not math.isfinite(result["audio_s"]) or result["audio_s"] <= 0:
        raise ValueError("Invalid reference audio duration")
    for mode in result["modes"]:
        if not isinstance(mode["key"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", mode["key"]):
            raise ValueError("Invalid mode key")
        if type(mode["threads"]) is not int or not 1 <= mode["threads"] <= 65535:
            raise ValueError("Invalid inference thread count")
        if not all(isinstance(mode[field], str) for field in ("label", "backend")):
            raise ValueError("Invalid mode description")
    modes = {mode["key"]: mode for mode in result["modes"]}
    if not modes or len(modes) != len(result["modes"]):
        raise ValueError("Missing or duplicate modes")
    if checks := result.get("output_checks"):
        if checks["reference_mode"] not in modes or set(checks["modes"]) != set(modes):
            raise ValueError("Output checks do not match measured modes")
        if type(checks["reference_deterministic"]) is not bool:
            raise ValueError("Invalid output repeatability result")
        optional_checks = [checks[key] for key in ("fixed_control_repeat", "xnnpack_vs_fixed_control", "xnnpack_reference_repeat", "combined_vs_xnnpack", "combined_vs_fixed_control") if key in checks]
        for check in [checks["reference_repeat"], *checks["modes"].values(), *optional_checks]:
            if check["finite"] is not True or check["fp32_samples"] <= 0:
                raise ValueError("Invalid FP32 output check")
            for field in ("pcm_exact", "fp32_exact"):
                if type(check[field]) is not bool:
                    raise ValueError("Invalid output exactness result")
            for field in ("pcm_changed_samples", "pcm_max_abs_lsb", "pcm_rmse_lsb", "fp32_changed_samples", "fp32_max_abs", "fp32_rmse", "fp32_relative_rmse"):
                if not isinstance(check[field], (int, float)) or not math.isfinite(check[field]) or check[field] < 0:
                    raise ValueError("Invalid output error statistic")
    by_block: dict[tuple[str, int], list[dict[str, Any]]] = {}
    orders = []
    for trial in result["trials"]:
        if trial["mode"] not in modes or trial["threads"] != modes[trial["mode"]]["threads"]:
            raise ValueError("Trial does not match its configured mode")
        for field in ("elapsed_s", "audio_s", "rtf"):
            if not isinstance(trial[field], (int, float)) or not math.isfinite(trial[field]) or trial[field] <= 0:
                raise ValueError(f"Invalid {field}")
        if not math.isclose(trial["rtf"], trial["elapsed_s"] / trial["audio_s"], rel_tol=1e-9):
            raise ValueError("RTF does not match measured duration")
        for field in ("cpu_time_s", "cpu_window_s", "cpu_avg_cores", "cpu_percent"):
            if not isinstance(trial[field], (float, int)) or not math.isfinite(trial[field]) or trial[field] < 0:
                raise ValueError(f"Invalid {field}")
        if trial["cpu_window_s"] <= 0 or type(trial["cpu_incomplete"]) is not bool:
            raise ValueError("Invalid CPU observation window/coverage")
        for field in ("cpu_samples", "cpu_processes"):
            if type(trial[field]) is not int or trial[field] < 1:
                raise ValueError(f"Invalid {field}")
        if not math.isclose(trial["cpu_avg_cores"], trial["cpu_time_s"] / trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("CPU core equivalent does not match the observation window")
        if not math.isclose(trial["cpu_percent"], 100 * trial["cpu_avg_cores"], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("CPU percent does not match the one-core normalization")
        if result["schema_version"] >= 3:
            trace = trial["cpu_trace"]
            if not trace or len(trace) != trial["cpu_samples"] - 1:
                raise ValueError("CPU trace does not match the number of snapshots")
            previous_end = None
            incomplete = False
            phases = []
            for point in trace:
                phase = point.get("phase", "action")
                if phase not in ("pre", "action", "post"):
                    raise ValueError("Invalid CPU observation phase")
                if phases and phase != phases[-1]:
                    incomplete = False
                for key in ("start_s", "end_s", "cpu_time_s", "cpu_percent"):
                    if not isinstance(point[key], (int, float)) or not math.isfinite(point[key]):
                        raise ValueError(f"Invalid CPU interval {key}")
                span = point["end_s"] - point["start_s"]
                if span <= 0 or point["cpu_time_s"] < 0 or point["cpu_percent"] < 0:
                    raise ValueError("Invalid CPU interval duration/counter")
                if previous_end is not None and not math.isclose(point["start_s"], previous_end, abs_tol=1e-9):
                    raise ValueError("CPU intervals must be contiguous")
                if not math.isclose(point["cpu_percent"], 100 * point["cpu_time_s"] / span, rel_tol=1e-8, abs_tol=1e-8):
                    raise ValueError("CPU interval percent does not match its actual duration")
                if type(point["cpu_processes"]) is not int or not 1 <= point["cpu_processes"] <= trial["cpu_processes"]:
                    raise ValueError("Invalid CPU interval process count")
                if type(point["cpu_incomplete"]) is not bool or incomplete and not point["cpu_incomplete"]:
                    raise ValueError("CPU interval coverage cannot recover after a lost process")
                incomplete = point["cpu_incomplete"]
                previous_end = point["end_s"]
                phases.append(phase)
            action_trace = [point for point in trace if point.get("phase", "action") == "action"]
            if not action_trace or action_trace[-1]["cpu_incomplete"] != trial["cpu_incomplete"]:
                raise ValueError("Action CPU coverage does not match its headline result")
            if not action_trace or not math.isclose(sum(point["cpu_time_s"] for point in action_trace), trial["cpu_time_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Action CPU intervals do not sum to headline CPU time")
            if not math.isclose(sum(point["end_s"] - point["start_s"] for point in action_trace), trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("Action CPU intervals do not match headline CPU window")
            if result["schema_version"] >= 4:
                if phases != sorted(phases, key={"pre": 0, "action": 1, "post": 2}.get) or set(phases) != {"pre", "action", "post"}:
                    raise ValueError("CPU trace must contain ordered pre/action/post phases")
                for phase in ("pre", "post"):
                    seconds = sum(point["end_s"] - point["start_s"] for point in trace if point["phase"] == phase)
                    if seconds < CPU_PADDING_S - 1e-6:
                        raise ValueError("CPU padding does not contain a full observed second")
        by_block.setdefault((trial["mode"], trial["block"]), []).append(trial)
        orders.append(trial["order"])
    if sorted(orders) != list(range(1, len(orders) + 1)):
        raise ValueError("Trial order must be unique and consecutive")
    for mode in modes:
        for block in range(1, BLOCKS_PER_MODE + 1):
            trials = by_block.get((mode, block), [])
            if sorted(row["trial"] for row in trials) != list(range(1, TRIALS_PER_BLOCK + 1)):
                raise ValueError(f"Incomplete block: {mode} / {block}")
            positions = sorted(row["order"] for row in trials)
            if positions != list(range(positions[0], positions[0] + TRIALS_PER_BLOCK)):
                raise ValueError("Trials within a block must be consecutive")
    if len(by_block) != len(modes) * BLOCKS_PER_MODE:
        raise ValueError("Unexpected block")
    if "schedule" in result:
        actual = [{"mode": row["mode"], "number": row["block"]} for row in sorted(result["trials"], key=lambda row: row["order"]) if row["trial"] == 1]
        if actual != result["schedule"]:
            raise ValueError("Recorded block schedule differs from actual trials")
        if result.get("schedule_method", "").startswith("seeded position-balanced"):
            for mode in modes:
                positions = [index % len(modes) for index, block in enumerate(actual) if block["mode"] == mode]
                counts = [positions.count(position) for position in range(len(modes))]
                if max(counts) - min(counts) > 1:
                    raise ValueError("Settings pass is not position-balanced")


def run_schedule(modes: list[Mode], seed: int,
                 synthesize: Callable[[Mode], tuple[float, float, CpuMeasurement]],
                 log: list[dict[str, Any]], *, balanced: bool = False) -> list[Trial]:
    """Only one callback runs at a time; all model initialization is external."""
    schedule = make_schedule(modes, seed, balanced=balanced)
    indexed = {mode.key: mode for mode in modes}
    trials = []
    for block in schedule:
        mode = indexed[block.mode]
        progress(f"{mode.label}: block {block.number}/{BLOCKS_PER_MODE}")
        for repetition in range(1, TRIALS_PER_BLOCK + 1):
            elapsed, duration, cpu = synthesize(mode)
            if not (math.isfinite(elapsed) and elapsed > 0 and math.isfinite(duration) and duration > 0):
                raise RuntimeError(f"{mode.label} returned an invalid measurement")
            trials.append(Trial(len(trials) + 1, mode.key, block.number, repetition,
                                mode.threads, elapsed, duration, elapsed / duration, **asdict(cpu)))
            coverage = " (CPU incomplete)" if cpu.cpu_incomplete else ""
            progress(f"Trial {len(trials)}/{len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK}: {mode.label}, block {block.number}, {repetition}/{TRIALS_PER_BLOCK}; synthesis {elapsed:.3f}s, CPU {cpu.cpu_percent:.1f}%{coverage}")
        log.append({"event": "block_completed", "mode": mode.key,
                    "block": block.number, "samples": TRIALS_PER_BLOCK})
    return trials


def svg_results(result: dict[str, Any]) -> str:
    modes = result["modes"]
    rows = result["trials"]
    width, height = 880, max(230, 85 + 66 * len(modes))
    left, right, top, bottom = 220, 30, 24, 45
    maximum = max(row["elapsed_s"] for row in rows) * 1.12
    plot_width = width - left - right
    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]
    output = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="各モードの合成時間。点は全試行、太線は中央値">']
    for tick in range(6):
        value = maximum * tick / 5
        x = left + value / maximum * plot_width
        output.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{height-bottom}" stroke="#e2e6ec"/>')
        output.append(f'<text x="{x:.2f}" y="{height-20}" text-anchor="middle">{value:.2f}</text>')
    for index, mode in enumerate(modes):
        y = top + 33 + index * 66
        samples = [row for row in rows if row["mode"] == mode["key"]]
        color = colors[index % len(colors)]
        output.append(f'<text x="{left-15}" y="{y+5}" text-anchor="end">{html.escape(mode["label"])}</text>')
        for number, sample in enumerate(samples):
            x = left + sample["elapsed_s"] / maximum * plot_width
            dy = ((number % 5) - 2) * 5
            label = html.escape(f'Block {sample["block"]}, trial {sample["trial"]}: {sample["elapsed_s"]:.6f} s, RTF {sample["rtf"]:.6f}')
            output.append(f'<circle cx="{x:.2f}" cy="{y+dy}" r="3.5" fill="{color}" fill-opacity="0.65"><title>{label}</title></circle>')
        median = statistics.median(row["elapsed_s"] for row in samples)
        x = left + median / maximum * plot_width
        output.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y-17}" y2="{y+17}" stroke="{color}" stroke-width="3"><title>Median: {median:.6f} s</title></line>')
    output.append(f'<text x="{left+plot_width/2}" y="{height-2}" text-anchor="middle">合成時間（秒、短いほど速い）</text></svg>')
    return "".join(output)


def svg_sequence(result: dict[str, Any]) -> str:
    rows = sorted(result["trials"], key=lambda row: row["order"])
    modes = result["modes"]
    colors = {mode["key"]: color for mode, color in zip(modes, ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"] * len(modes))}
    width, height, left, top = 880, 180, 65, 15
    plot_width, plot_height = width - left - 25, height - top - 40
    maximum = max(row["rtf"] for row in rows) * 1.1
    output = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="測定順とRTF">']
    for tick in range(4):
        value = maximum * tick / 3
        y = top + plot_height - value / maximum * plot_height
        output.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-25}" y2="{y:.2f}" stroke="#e2e6ec"/><text x="{left-10}" y="{y+4:.2f}" text-anchor="end">{value:.2f}</text>')
    for row in rows:
        x = left + (row["order"] - 1) / max(1, len(rows) - 1) * plot_width
        y = top + plot_height - row["rtf"] / maximum * plot_height
        output.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{colors[row["mode"]]}"><title>#{row["order"]}: {html.escape(row["mode"])} / RTF {row["rtf"]:.6f}</title></circle>')
    output.append(f'<text x="{left}" y="{height-18}" text-anchor="middle">1</text><text x="{width-25}" y="{height-18}" text-anchor="middle">{len(rows)}</text><text x="{width/2}" y="{height-2}" text-anchor="middle">測定順（縦軸：RTF）</text></svg>')
    return "".join(output)


def aggregate_cpu_profiles(result: dict[str, Any], bin_seconds: float = 0.25) -> list[dict[str, Any]]:
    """Start-aligned pre/action and separately end-aligned actual post samples."""
    if not math.isfinite(bin_seconds) or bin_seconds <= 0:
        raise ValueError("CPU profile bin duration must be positive")
    profiles = []
    padded = result["schema_version"] >= 4
    for mode in result["modes"]:
        all_trials = [row for row in result["trials"] if row["mode"] == mode["key"]]
        phase_counts = {phase: 0 for phase in ("pre", "action", "post")}
        main_segments, post_segments = [], []
        for trial in all_trials:
            main, post = [], []
            for phase in phase_counts:
                intervals = [point for point in trial.get("cpu_trace", []) if point.get("phase", "action") == phase]
                if not intervals or any(point["cpu_incomplete"] for point in intervals):
                    continue
                phase_counts[phase] += 1
                origin = intervals[0]["start_s"] if phase == "post" else 0.0
                for point in intervals:
                    start, end = point["start_s"] - origin, point["end_s"] - origin
                    rate = point["cpu_time_s"] / (point["end_s"] - point["start_s"])
                    if padded and phase == "action":
                        start = max(0.0, start)
                    elif phase == "pre":
                        end = min(0.0, end)
                    (post if phase == "post" else main).append((start, end, rate))
            main_segments.append(main)
            post_segments.append(post)
        maximum = max((end for trial in main_segments for _, end, _ in trial), default=0.0)

        def bin_segments(segments: list[list[tuple[float, float, float]]], minimum: float, maximum: float) -> list[dict[str, Any]]:
            count = max(0, math.ceil((maximum - minimum) / bin_seconds))
            contributions: list[list[tuple[float, float]]] = [[] for _ in range(count)]
            for trial in segments:
                cpu, observed = [0.0] * count, [0.0] * count
                for start, end, rate in trial:
                    start, end = max(minimum, start), min(maximum, end)
                    index = max(0, math.floor((start - minimum) / bin_seconds))
                    while index < count and minimum + index * bin_seconds < end:
                        left = minimum + index * bin_seconds
                        overlap = max(0.0, min(end, left + bin_seconds) - max(start, left))
                        cpu[index] += rate * overlap
                        observed[index] += overlap
                        index += 1
                for index, seconds in enumerate(observed):
                    if seconds > 0:
                        contributions[index].append((cpu[index], seconds))
            points = []
            for index, entries in enumerate(contributions):
                if not entries:
                    continue
                cpu, seconds = math.fsum(pair[0] for pair in entries), math.fsum(pair[1] for pair in entries)
                percents = [100 * pair[0] / pair[1] for pair in entries]
                quartiles = statistics.quantiles(percents, n=4, method="inclusive") if len(percents) > 1 else [percents[0]] * 3
                points.append({"start_s": minimum + index * bin_seconds, "end_s": min(maximum, minimum + (index + 1) * bin_seconds),
                               "cpu_percent": 100 * cpu / seconds, "min_percent": min(percents), "max_percent": max(percents),
                               "median_percent": statistics.median(percents), "q25_percent": quartiles[0], "q75_percent": quartiles[2],
                               "cpu_time_s": cpu, "observed_s": seconds, "trial_count": len(entries)})
            return points
        profiles.append({"mode": mode["key"], "label": mode["label"], "complete_trials": phase_counts["action"],
                         "phase_complete_trials": phase_counts, "total_trials": len(all_trials), "bin_seconds": bin_seconds,
                         "points": bin_segments(main_segments, -CPU_PADDING_S if padded else 0.0, maximum),
                         "post_points": bin_segments(post_segments, 0.0, CPU_PADDING_S) if padded else []})
    return profiles


def cpu_profile_html(result: dict[str, Any]) -> str:
    if result["schema_version"] < 3:
        return ""
    profiles = aggregate_cpu_profiles(result)
    padded = result["schema_version"] >= 4
    xmin = -CPU_PADDING_S if padded else 0.0
    xmax = max((point["end_s"] for row in profiles for point in row["points"]), default=0.25)
    ymax = max(100, math.ceil(max((point["q75_percent"] for row in profiles for point in row["points"] + row["post_points"]), default=0) / 100) * 100)
    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873", "#303030"]
    figures = []
    for index, profile in enumerate(profiles):
        left, top, width, height = 52, 30, 255 if padded else 365, 140
        def x(value: float) -> float:
            return left + (value - xmin) / (xmax - xmin) * width
        def post_x(value: float) -> float:
            return left + width + 30 + value / CPU_PADDING_S * 80
        def y(value: float) -> float:
            return top + height - value / ymax * height
        color = colors[index % len(colors)]
        parts = [f'<svg class="cpu-profile" data-mode="{profile["mode"]}" viewBox="0 0 440 215" role="img" aria-label="{html.escape(profile["label"], quote=True)} CPU時系列集計"><title>{html.escape(profile["label"])}: 完全追跡 {profile["complete_trials"]}/{profile["total_trials"]}回</title>',
                 f'<text x="{left}" y="16">{html.escape(profile["label"])}</text>']
        if padded:
            parts.append(f'<rect x="{left}" y="{top}" width="{x(0)-left:.2f}" height="{height}" fill="#edf0f5"/><line x1="{x(0):.2f}" x2="{x(0):.2f}" y1="{top}" y2="{top+height}" stroke="#8592a3" stroke-dasharray="3 2"><title>合成要求開始（0秒）</title></line>')
        for tick in range(3):
            value = ymax * tick / 2
            parts.append(f'<line x1="{left}" y1="{y(value):.2f}" x2="{left+width}" y2="{y(value):.2f}" stroke="#e2e6ec"/><text x="{left-7}" y="{y(value)+4:.2f}" text-anchor="end">{value:.0f}%</text>')
            if padded:
                parts.append(f'<line x1="{post_x(0):.2f}" y1="{y(value):.2f}" x2="{post_x(CPU_PADDING_S):.2f}" y2="{y(value):.2f}" stroke="#e2e6ec"/>')
        for tick in range(5):
            value = xmax * tick / 4
            parts.append(f'<text x="{x(value):.2f}" y="190" text-anchor="middle">{value:.1f}</text>')
        if padded:
            for value in (0, CPU_PADDING_S):
                parts.append(f'<text x="{post_x(value):.2f}" y="190" text-anchor="middle">{value:.0f}</text>')
        for series, mapper, prefix in ((profile["points"], x, "開始基準"), (profile["post_points"], post_x, "応答完了基準")):
            previous = None
            previous_end = None
            for point in series:
                low_count = point["trial_count"] < profile["total_trials"]
                opacity = "0.45" if low_count else "1"
                dash = ' stroke-dasharray="3 2"' if low_count else ""
                a, b = mapper(point["start_s"]), mapper(point["end_s"])
                parts.append(f'<rect x="{a:.2f}" y="{y(point["q75_percent"]):.2f}" width="{b-a:.2f}" height="{y(point["q25_percent"])-y(point["q75_percent"]):.2f}" fill="{color}" fill-opacity="{0.07 if low_count else 0.15}"/>')
                path = f'M{a:.2f},{y(point["median_percent"]):.2f}L{b:.2f},{y(point["median_percent"]):.2f}'
                if previous is not None and math.isclose(previous_end, point["start_s"], abs_tol=1e-9):
                    path = f'M{a:.2f},{y(previous):.2f}L{a:.2f},{y(point["median_percent"]):.2f}' + path
                parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" opacity="{opacity}"{dash}/>')
                title = f'{prefix} {point["start_s"]:.2f}–{point["end_s"]:.2f}秒: 中央値 {point["median_percent"]:.1f}%, 四分位 {point["q25_percent"]:.1f}–{point["q75_percent"]:.1f}%, 寄与試行数 {point["trial_count"]}'
                parts.append(f'<rect x="{a:.2f}" y="{top}" width="{max(1,b-a):.2f}" height="{height}" fill="transparent"><title>{html.escape(title)}</title></rect>')
                previous, previous_end = point["median_percent"], point["end_s"]
        main_caption = "開始前1秒〜合成中（秒）" if padded else "合成要求開始からの秒数（共通軸）"
        parts.append(f'<text x="{left+width/2}" y="211" text-anchor="middle">{main_caption}</text>')
        if padded:
            parts.append(f'<text x="{post_x(0.5):.2f}" y="211" text-anchor="middle">完了後（秒）</text>')
        parts.append('</svg>')
        tail = profile["points"][-1]["trial_count"] if profile["points"] else 0
        counts = profile["phase_complete_trials"]
        caption = (f'完全追跡 前 {counts["pre"]}・合成中 {counts["action"]}・後 {counts["post"]}/{profile["total_trials"]}回' if padded
                   else f'完全追跡 {profile["complete_trials"]}/{profile["total_trials"]}回 · 最終区間の寄与 {tail}回')
        figures.append('<figure>' + ''.join(parts) + f'<figcaption>{caption}</figcaption></figure>')
    payload = json.dumps(profiles, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    padding_note = "左は開始前1秒と合成中、右は各試行の応答完了にそろえた後1秒。前後も実測で、平均CPU表には合成中だけを使用。" if padded else ""
    return '<h2>CPU 使用率の時間推移（モード別集計）</h2><div class="cpu-grid">' + ''.join(figures) + '</div><p class="cpu-caption">' + padding_note + '線は完全追跡できた区間の試行間中央値、帯は四分位範囲。全モード共通軸・0.25秒ビン。100%＝論理1コア。観測が終わった試行は0埋めせず除外し、寄与数が減る区間は薄い破線。各区間にカーソルを重ねると寄与数を表示</p><script id="cpu-profile-data" type="application/json">' + payload + '</script>'


def render_report(result: dict[str, Any], target: Path) -> None:
    validate_results(result)
    raw_csv = trial_csv(result["trials"])
    profile = cpu_profile_html(result)
    profile_csv = (f'<details><summary>CPU 時系列（CSV）</summary><button data-csv="cpu-csv" data-filename="voicevox-cpu-timeseries.csv" type="button">CSV を保存</button><pre id="cpu-csv">{html.escape(cpu_trace_csv(result["trials"]))}</pre></details>'
                   if result["schema_version"] >= 3 else "")
    summaries = []
    checks = result.get("output_checks")
    output_header = "<th>PCM / FP32</th>" if checks else ""
    for mode in result["modes"]:
        trials = [row for row in result["trials"] if row["mode"] == mode["key"]]
        cpu_percent = weighted_cpu_percent(trials)
        output_cell = ""
        if checks:
            check = checks["modes"][mode["key"]]
            status = ("一致" if check["pcm_exact"] else "差分あり") + " / " + ("一致" if check["fp32_exact"] else "差分あり")
            if not checks["reference_deterministic"]:
                status = "基準が非決定的"
            output_cell = f"<td>{status}</td>"
        summaries.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            mode["label"], mode["threads"], len(trials),
            f'{statistics.median(row["elapsed_s"] for row in trials):.3f}',
            f'{statistics.median(row["rtf"] for row in trials):.3f}',
            "—" if cpu_percent is None else f"{cpu_percent:.1f}%",
            f'{min(row["elapsed_s"] for row in trials):.3f}–{max(row["elapsed_s"] for row in trials):.3f}',
        )) + output_cell + "</tr>")
    env_rows = "".join(f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>" for key, value in result["environment"].items())
    mode_rows = "".join(f'<li>{html.escape(mode["label"])}: {mode["threads"]} inference thread(s), {html.escape(mode["backend"])}</li>' for mode in result["modes"])
    note_values = list(result.get("notes", []))
    incomplete = sum(row["cpu_incomplete"] for row in result["trials"])
    if incomplete:
        note_values.append(f"CPU追跡が不完全な{incomplete}試行はCPU平均から除外。試行のCPU総量は観測できた下限値で、時系列では区間への帰属にも不確実性あり。")
    if checks and not checks["reference_deterministic"]:
        note_values.append("基準の繰り返し出力が一致しないため、この実行では精度維持の判定を行わない。")
    output_details = (f'<details><summary>出力一致の検証</summary><p>同じ実行の {html.escape(checks["reference_mode"])} が基準。PCMは整数16bit、FP32はPCM化前。完全一致のみ一致と表示し、誤差の許容しきい値は設定しない。</p><pre>{html.escape(json.dumps(checks, ensure_ascii=False, indent=2))}</pre></details>'
                      if checks else "")
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in note_values)
    duration = result["audio_s"]
    log_lines = "\n".join(json.dumps(event, ensure_ascii=False) for event in result.get("log", []))
    ordering = ("乱数で基本順とラウンド順を決め、各位置の回数差を最大1に制限。各ペアの前後順は両方を含む" if result.get("schedule_method", "").startswith("seeded position-balanced")
                else "各ラウンドでモード順をシャッフル")
    document = f'''<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOICEVOX CORE benchmark</title>
<style>
:root{{font-family:system-ui,-apple-system,sans-serif;color:#202936;background:#fff;font-size:15px;line-height:1.5}}body{{max-width:960px;margin:36px auto;padding:0 24px}}h1{{font-size:24px;letter-spacing:-.03em;margin:0 0 6px}}h2{{font-size:17px;margin:28px 0 10px}}p{{margin:6px 0 18px;color:#546170}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 12px;border-bottom:1px solid #e2e6ec;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{font-weight:600;background:#f6f8fa}}figure{{margin:18px 0}}figcaption{{color:#546170;font-size:13px}}svg{{width:100%;height:auto}}svg text{{font-size:12px;fill:#546170}}details{{margin:18px 0;border-top:1px solid #d8dee7;padding-top:12px}}summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f6f8fa;padding:14px;max-height:420px;overflow:auto}}button{{font:inherit;background:#fff;border:1px solid #a8b4c4;border-radius:4px;padding:6px 12px;cursor:pointer;margin-top:12px}}.cpu-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px 12px}}.cpu-grid figure{{margin:0}}.cpu-caption{{font-size:12px;margin-top:12px}}.scroll{{overflow-x:auto}}.environment th{{text-align:left;width:34%}}.environment td{{text-align:left;overflow-wrap:anywhere}}ul{{padding-left:22px}}@media(max-width:600px){{.cpu-grid{{grid-template-columns:1fr}}body{{margin:20px auto;padding:0 14px}}th,td{{padding:7px}}}}
</style>
<h1>VOICEVOX CORE benchmark</h1>
<p>{html.escape(result["created_at"])} · 音声 {duration:.3f} 秒 · 各モード 5 回 × 3 ブロック</p>
<div class="scroll"><table><thead><tr><th>モード</th><th>スレッド</th><th>回数</th><th>中央値（秒）</th><th>中央値 RTF</th><th>平均 CPU</th><th>最小–最大（秒）</th>{output_header}</tr></thead><tbody>{''.join(summaries)}</tbody></table></div>
<figure>{svg_results(result)}<figcaption>点は各試行、太線は中央値。RTF = 合成時間 ÷ 出力音声の長さ</figcaption></figure>
<figure>{svg_sequence(result)}</figure>
{profile}
<h2>測定条件</h2>
<ul><li>同一 sample.vvm・Style ID {result["style_id"]}・準備済み AudioQuery JSON を使用。CPU 推論のみ、辞書・テキスト解析なし</li>
<li>モデル初期化・ダウンロード・ビルド・AudioQuery 作成・ウォームアップ・音声保存は測定外。合成から WAV 生成までを測定</li>
<li>{ordering}。1 ブロック内は 5 回連続。3 ラウンド、seed = {result["seed"]}。モード間の同時実行なし</li>
<li>表示スレッド数は推論に設定した値。Web Worker 数ではない</li>
<li>CPU は対象プロセスと子孫の user＋system 時間 ÷ カウンター採取間の観測時間。100%＝論理1コア、各試行の時間で重み付けした平均</li>
<li>browser はモード別 Chromium 全体を集計（Python・制御用 Node は除外）。100ms ごとに追跡し、短命プロセスは取りこぼす場合あり</li>{mode_rows}{notes}</ul>
<details><summary>実行環境・ログ</summary><table class="environment">{env_rows}</table><pre>{html.escape(log_lines)}</pre></details>
{output_details}
<details><summary>生データ（CSV）</summary><button id="save-csv" data-csv="raw-csv" data-filename="voicevox-benchmark.csv" type="button">CSV を保存</button><pre id="raw-csv">{html.escape(raw_csv)}</pre></details>
{profile_csv}
<script>
document.querySelectorAll('button[data-csv]').forEach(button=>button.addEventListener('click',()=>{{
 const blob=new Blob([document.getElementById(button.dataset.csv).textContent],{{type:'text/csv;charset=utf-8'}});
 const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=button.dataset.filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}}));
</script></html>'''
    atomic_write(target, document.encode("utf-8"))


# The generated Rust program uses no text analyzer or dictionary on either target.
RUST_SOURCE = r'''
use anyhow::{Context as _, ensure};
use std::{io::{self, BufRead, Write}, sync::{Mutex, OnceLock}};
use voicevox_core::{AccelerationMode, AudioQuery, StyleId, VoiceModelId, blocking::{Onnxruntime, Synthesizer, VoiceModelFile}};
static SYNTH: OnceLock<Synthesizer<()>> = OnceLock::new();
static QUERY: OnceLock<AudioQuery> = OnceLock::new();
static WAV: Mutex<Vec<u8>> = Mutex::new(Vec::new());
static RAW: Mutex<Vec<u8>> = Mutex::new(Vec::new());
static SPIN_OFF: OnceLock<bool> = OnceLock::new();
static FIXED_LENGTH: OnceLock<usize> = OnceLock::new();
static MODEL_ID: OnceLock<VoiceModelId> = OnceLock::new();
fn setup(runtime: &'static Onnxruntime, model: &str, query: &str, threads: u16, fixed_shape: bool, xnn_threads: u16, profile: bool) -> anyhow::Result<()> {
    let query: AudioQuery = serde_json::from_slice(&std::fs::read(query)?)?;
    query.validate()?;
    let padded_length = if fixed_shape { Some(Synthesizer::<()>::benchmark_vocoder_length(&query)?) } else { None };
    voicevox_core::__benchmark_fixed_shape::configure(padded_length, xnn_threads.into(), profile)?;
    let synth = Synthesizer::builder(runtime).acceleration_mode(AccelerationMode::Cpu).cpu_num_threads(if xnn_threads > 0 { 1 } else { threads }).build()?;
    let model = VoiceModelFile::open(model)?;
    MODEL_ID.set(model.id()).map_err(|_| anyhow::anyhow!("Model ID already set"))?;
    synth.load_voice_model(&model).perform()?;
    voicevox_core::__benchmark_fixed_shape::verify_loaded()?;
    FIXED_LENGTH.set(padded_length.unwrap_or(0)).map_err(|_| anyhow::anyhow!("Fixed-shape configuration already set"))?;
    SYNTH.set(synth).map_err(|_| anyhow::anyhow!("Already initialized"))?;
    QUERY.set(query).map_err(|_| anyhow::anyhow!("Already initialized"))?;
    Ok(())
}
fn synthesize(style: u32) -> anyhow::Result<()> {
    let synth = SYNTH.get().context("Not initialized")?;
    let query = QUERY.get().context("No AudioQuery")?;
    let wav = synth.synthesis(query, StyleId::new(style)).perform()?;
    ensure!(wav.len() >= 44 && &wav[..4] == b"RIFF" && &wav[8..12] == b"WAVE", "Invalid WAV");
    *WAV.lock().unwrap() = wav;
    Ok(())
}
fn raw_wave(style: u32) -> anyhow::Result<()> {
    let synth = SYNTH.get().context("Not initialized")?;
    let wave = synth.benchmark_raw_synthesis(QUERY.get().context("No AudioQuery")?, StyleId::new(style))?;
    ensure!(wave.iter().all(|sample| sample.is_finite()), "Nonfinite FP32 waveform");
    *RAW.lock().unwrap() = wave.iter().flat_map(|sample| sample.to_le_bytes()).collect();
    Ok(())
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_init(threads: u16, spin_off: bool, fixed_shape: bool, xnn_threads: u16, profile: bool) -> i32 {
    let result = browser_runtime(threads, spin_off, xnn_threads).and_then(|runtime| setup(runtime, "/sample.vvm", "/query.json", threads, fixed_shape, xnn_threads, profile));
    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
fn browser_runtime(threads: u16, spin_off: bool, xnn_threads: u16) -> anyhow::Result<&'static Onnxruntime> {
    #[cfg(feature="threaded")]
    {
        // ORT's pthread WASM build requires one global pool. Register it before
        // CORE creates its environment; the pinned ort crate automatically
        // disables per-session pools when a global pool is present.
        let base = unsafe { &*ort::sys::OrtGetApiBase() };
        let api = unsafe { (base.GetApi)(ort::sys::ORT_API_VERSION) };
        ensure!(!api.is_null(), "ORT API unavailable");
        ensure!(ort::set_api(unsafe { api.read() }), "ORT API already initialized");
        let pool = ort::environment::GlobalThreadPoolOptions::default()
            .with_intra_threads(if xnn_threads > 0 { 1 } else { threads.into() })?.with_inter_threads(1)?.with_spin_control(!spin_off)?;
        ensure!(ort::init().with_name("voicevox_benchmark").with_global_thread_pool(pool).commit(), "ORT environment already initialized");
        SPIN_OFF.set(spin_off).map_err(|_| anyhow::anyhow!("Spin configuration already set"))?;
    }
    #[cfg(not(feature="threaded"))]
    let _ = (threads, spin_off, xnn_threads);
    Ok(Onnxruntime::init_once()?)
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_synthesize(style: u32) -> i32 {
    match synthesize(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_wav_ptr() -> *const u8 { WAV.lock().unwrap().as_ptr() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_wav_len() -> usize { WAV.lock().unwrap().len() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw(style: u32) -> i32 {
    match raw_wave(style) { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw_ptr() -> *const u8 { RAW.lock().unwrap().as_ptr() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_raw_len() -> usize { RAW.lock().unwrap().len() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_spin_off() -> i32 { i32::from(SPIN_OFF.get().copied().unwrap_or(false)) }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_fixed_length() -> usize { FIXED_LENGTH.get().copied().unwrap_or(0) }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_fixed_matches() -> usize { voicevox_core::__benchmark_fixed_shape::verified_sessions() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_xnn_threads() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_threads() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_xnn_sessions() -> usize { voicevox_core::__benchmark_fixed_shape::xnn_sessions() }
#[cfg(target_os="emscripten")]
#[unsafe(no_mangle)]
pub extern "C" fn bench_finish_profile() -> i32 {
    let result = (|| -> anyhow::Result<()> {
        ensure!(voicevox_core::__benchmark_fixed_shape::profiling_enabled(), "Profiling is disabled");
        println!("BENCH_PROFILE_BEGIN"); io::stdout().flush()?;
        SYNTH.get().context("Not initialized")?.unload_voice_model(*MODEL_ID.get().context("No model ID")?)?;
        println!("BENCH_PROFILE_END"); io::stdout().flush()?;
        Ok(())
    })();
    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
fn main() {}
#[cfg(not(target_os="emscripten"))]
fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure!(args.len() == 7, "Expected runtime, model, query, threads, style, wav destination");
    let runtime = Onnxruntime::load_once().filename(&args[1]).perform()?;
    let threads = args[4].parse()?;
    let style = args[5].parse()?;
    setup(runtime, &args[2], &args[3], threads, false, 0, false)?;
    println!("{}", serde_json::json!({"ready": true, "threads": threads}));
    io::stdout().flush()?;
    for line in io::stdin().lock().lines() {
        let line = line?;
        if line == "quit" { break; }
        if line == "raw-save" {
            raw_wave(style)?;
            let raw = RAW.lock().unwrap();
            std::fs::write(std::path::Path::new(&args[6]).with_extension("f32"), &*raw)?;
            println!("{}", serde_json::json!({"raw_bytes": raw.len()}));
            io::stdout().flush()?;
            continue;
        }
        ensure!(line == "synthesize" || line == "synthesize-save", "Unknown request");
        let start = std::time::Instant::now();
        synthesize(style)?;
        let elapsed_s = start.elapsed().as_secs_f64();
        let wav = WAV.lock().unwrap();
        if line == "synthesize-save" { std::fs::write(&args[6], &*wav)?; }
        println!("{}", serde_json::json!({"elapsed_s": elapsed_s, "wav_bytes": wav.len()}));
        io::stdout().flush()?;
    }
    Ok(())
}
'''

BROWSER_WORKER = r'''
let configuredThreads=1, threaded=false, ready=false, wasmStdout=[];
function failure(error){postMessage({error:String(error && error.stack || error)});}
onmessage=async ({data})=>{
 try{
  if(data.command==='init'){
   configuredThreads=data.threads;threaded=data.threaded;
   if(data.threaded && (!self.crossOriginIsolated || typeof SharedArrayBuffer==='undefined'))
    throw new Error('Multithreaded WASM requires cross-origin isolation and SharedArrayBuffer');
   globalThis.Module={noInitialRun:true,benchmarkPoolSize:Math.max(1,configuredThreads),mainScriptUrlOrBlob:new URL(data.module,self.location).href,
    locateFile:(path)=>new URL(path,new URL(data.module,self.location)).href,
    printErr:(...values)=>postMessage({log:values.join(' ')}),
    print:(...values)=>wasmStdout.push(values.join(' ')),
    onAbort:failure,
    onRuntimeInitialized:async()=>{
     try{
      for(const file of ['sample.vvm','query.json']){
       const response=await fetch('/'+file);if(!response.ok)throw new Error('HTTP '+response.status+' '+file);
       Module.FS.writeFile('/'+file,new Uint8Array(await response.arrayBuffer()));
      }
      if(Module._bench_init(configuredThreads,Number(data.spin_off),Number(data.fixed_shape),data.xnn_threads||0,Number(data.profile))!==0)throw new Error('CORE initialization failed');
      Module.FS.unlink('/sample.vvm');Module.FS.unlink('/query.json');ready=true;
      const pthreads=threaded ? Module.PThread.runningWorkers.length : 0;
      if(threaded && configuredThreads>1 && pthreads<configuredThreads-1)
       throw new Error('ORT did not create the requested inference pthreads');
      postMessage({ready:true,threads:configuredThreads,shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads_created:pthreads,spin_off:Boolean(Module._bench_spin_off()),fixed_length:Module._bench_fixed_length(),fixed_matches:Module._bench_fixed_matches(),xnn_threads:Module._bench_xnn_threads(),xnn_sessions:Module._bench_xnn_sessions()});
     }catch(error){failure(error);}
    }};
   importScripts(data.module);
  }else if(data.command==='synthesize'){
   if(!ready)throw new Error('CORE not ready');
   const start=performance.now(),code=Module._bench_synthesize(data.style);
   const elapsed_s=(performance.now()-start)/1000;
   if(code!==0)throw new Error('Synthesis failed: '+code);
   const ptr=Module._bench_wav_ptr(),length=Module._bench_wav_len();
   if(!ptr||length<44)throw new Error('Invalid WAV');
   const result={elapsed_s,wav_bytes:length};
   if(data.save){result.wav=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage(result,[result.wav]);}
   else postMessage(result);
  }else if(data.command==='raw'){
   if(!ready || Module._bench_raw(data.style)!==0)throw new Error('Untimed FP32 synthesis failed');
   const ptr=Module._bench_raw_ptr(),length=Module._bench_raw_len();
   if(!ptr||length<4||length%4)throw new Error('Invalid FP32 output');
   const raw=Module.HEAPU8.slice(ptr,ptr+length).buffer;postMessage({raw},[raw]);
  }else if(data.command==='finish-profile'){
   const start=wasmStdout.length;
   if(Module._bench_finish_profile()!==0)throw new Error('Failed to flush untimed provider profile');
   ready=false;postMessage({profile_text:wasmStdout.slice(start).join('\n')});
  }else if(data.command==='thread-state'){
   if(!ready)throw new Error('CORE not ready');
   postMessage({pthreads:threaded ? Module.PThread.runningWorkers.length : 0});
  }
 }catch(error){failure(error);}
};
'''

BROWSER_PAGE = r'''<!doctype html><meta charset="utf-8"><title>VOICEVOX benchmark worker</title>
<script>
let worker=null, pending=null;
window.logs=[];
window.request=(data)=>new Promise((resolve,reject)=>{
 if(pending) return reject(new Error('Concurrent requests are forbidden'));
 if(data.command==='init'){
  worker=new Worker('/worker.js');
  worker.onerror=(event)=>{if(pending){clearTimeout(pending.timer);pending.reject(new Error(event.message));pending=null;worker.terminate();}};
  worker.onmessage=({data})=>{
   if(data.log){logs.push(data.log);return;}
   if(!pending)return;
   const {resolve,reject,timer}=pending;clearTimeout(timer);pending=null;
   if(data.error)reject(new Error(data.error+'\n'+logs.slice(-20).join('\n')));
   else {if(data.wav)data.wav=Array.from(new Uint8Array(data.wav));if(data.raw)data.raw=Array.from(new Uint8Array(data.raw));resolve(data);}
  };
 }
 const timer=setTimeout(()=>{worker.terminate();pending=null;reject(new Error('Worker timed out after 10 minutes'));},600000);
 pending={resolve,reject,timer};worker.postMessage(data);
});
</script>'''


def prepared_query(target_seconds: float) -> dict[str, Any]:
    """Hand-authored benchmark phonemes, independent of text analysis/models.

    Repeated /konnichiwa/ is intentionally a fixed synthetic query, not claimed
    to represent a natural 10-second utterance. Measured WAV duration is used
    for RTF rather than assuming the requested duration is exact.
    """
    if not math.isfinite(target_seconds) or not 1 <= target_seconds <= 60:
        raise ValueError("--target-seconds must be between 1 and 60")
    phrase = {"moras": [
        {"text": "コ", "consonant": "k", "consonant_length": .07, "vowel": "o", "vowel_length": .12, "pitch": 5.4},
        {"text": "ン", "consonant": None, "consonant_length": None, "vowel": "N", "vowel_length": .12, "pitch": 5.6},
        {"text": "ニ", "consonant": "n", "consonant_length": .06, "vowel": "i", "vowel_length": .12, "pitch": 5.6},
        {"text": "チ", "consonant": "ch", "consonant_length": .07, "vowel": "i", "vowel_length": .12, "pitch": 5.6},
        {"text": "ワ", "consonant": "w", "consonant_length": .06, "vowel": "a", "vowel_length": .14, "pitch": 5.3}],
        "accent": 5, "pause_mora": {"text": "、", "consonant": None, "consonant_length": None, "vowel": "pau", "vowel_length": .15, "pitch": 0.0}, "is_interrogative": False}
    phrases = [json.loads(json.dumps(phrase)) for _ in range(8)]
    total = .2 + sum(m["vowel_length"] + (m["consonant_length"] or 0) for p in phrases for m in p["moras"]) + 8 * .15
    return {"accent_phrases": phrases, "speedScale": total / target_seconds, "pitchScale": 0.0, "intonationScale": 1.0, "volumeScale": 1.0, "prePhonemeLength": .1, "postPhonemeLength": .1, "outputSamplingRate": 24000, "outputStereo": False}


CORE_BENCH_HELPER = '''        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch
        // up to decode(), before PCM conversion/resampling. Not called by synthesis().
        #[doc(hidden)]
        pub fn benchmark_raw_synthesis(&self, audio_query: &AudioQuery, style_id: StyleId) -> crate::Result<Vec<f32>> {
            let audio_query = audio_query.to_validated()?;
            let super::DecoderFeature { f0, phoneme } = audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);
            self.0.decode(f0.len(), super::PhonemeCode::num_phoneme(), &f0, phoneme.as_flattened(), style_id, ()).block_on()
        }

'''


CORE_FIXED_CONFIG = r'''
#[doc(hidden)]
pub mod __benchmark_fixed_shape {
    use std::sync::{
        OnceLock,
        atomic::{AtomicUsize, Ordering},
    };

    pub const VOCODER_SHA256_HEX: &str =
        "80a81fd0598b6e7d4e21fef74e03fed14e22f111c6b0f4c4454561baae075820";
    pub(crate) const VOCODER_SHA256: [u8; 32] = [0x80, 0xa8, 0x1f, 0xd0, 0x59, 0x8b, 0x6e, 0x7d, 0x4e, 0x21, 0xfe, 0xf7, 0x4e, 0x03, 0xfe, 0xd1, 0x4e, 0x22, 0xf1, 0x11, 0xc6, 0xb0, 0xf4, 0xc4, 0x45, 0x45, 0x61, 0xba, 0xae, 0x07, 0x58, 0x20];
    pub(crate) const VOCODER_BYTES: usize = 55_734_297;

    static REQUESTED: OnceLock<Option<usize>> = OnceLock::new();
    static VERIFIED_SESSIONS: AtomicUsize = AtomicUsize::new(0);
    static XNN_THREADS: OnceLock<usize> = OnceLock::new();
    static PROFILE: OnceLock<bool> = OnceLock::new();
    static XNN_SESSIONS: AtomicUsize = AtomicUsize::new(0);

    pub fn configure(padded_length: Option<usize>, xnn_threads: usize, profile: bool) -> anyhow::Result<()> {
        anyhow::ensure!((xnn_threads == 0 && !profile) || padded_length.is_some(), "Provider diagnostics require verified fixed vocoder shape");
        if let Some(n) = padded_length {
            anyhow::ensure!(n > 0, "Fixed vocoder length must be positive");
            let _ = i64::try_from(n)?;
        }
        REQUESTED
            .set(padded_length)
            .map_err(|_| anyhow::anyhow!("Benchmark configuration already initialized"))?;
        XNN_THREADS.set(xnn_threads).map_err(|_| anyhow::anyhow!("XNNPACK already configured"))?;
        PROFILE.set(profile).map_err(|_| anyhow::anyhow!("Profiling already configured"))?;
        Ok(())
    }

    pub fn xnn_threads() -> usize { XNN_THREADS.get().copied().unwrap_or(0) }
    pub fn profiling_enabled() -> bool { PROFILE.get().copied().unwrap_or(false) }
    pub fn xnn_sessions() -> usize { XNN_SESSIONS.load(Ordering::SeqCst) }
    pub(crate) fn record_xnn_session() -> anyhow::Result<()> {
        anyhow::ensure!(XNN_SESSIONS.fetch_add(1, Ordering::SeqCst) == 0, "Multiple XNNPACK sessions registered");
        Ok(())
    }

    pub(crate) fn requested_length() -> Option<usize> {
        REQUESTED.get().copied().flatten()
    }

    pub(crate) fn record_verified_session() -> anyhow::Result<()> {
        let old = VERIFIED_SESSIONS.fetch_add(1, Ordering::SeqCst);
        anyhow::ensure!(old == 0, "More than one fixed-shape vocoder session matched");
        Ok(())
    }

    pub fn verified_sessions() -> usize {
        VERIFIED_SESSIONS.load(Ordering::SeqCst)
    }

    pub fn verify_loaded() -> anyhow::Result<()> {
        let requested = REQUESTED
            .get()
            .ok_or_else(|| anyhow::anyhow!("Benchmark configuration was not initialized"))?;
        let expected = usize::from(requested.is_some());
        anyhow::ensure!(
            verified_sessions() == expected,
            "Expected {} verified fixed-shape vocoder session(s), got {}",
            expected,
            verified_sessions()
        );
        anyhow::ensure!(xnn_sessions() == usize::from(xnn_threads() > 0), "XNNPACK vocoder registration count mismatch");
        Ok(())
    }
}
'''

CORE_FIXED_HELPER = r'''
        #[doc(hidden)]
        pub fn benchmark_vocoder_length(audio_query: &AudioQuery) -> anyhow::Result<usize> {
            let audio_query = audio_query.to_validated()?;
            let super::DecoderFeature { f0, phoneme } =
                audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);
            let length = f0.len();
            let phoneme_size = super::PhonemeCode::num_phoneme();
            anyhow::ensure!(length > 0 && phoneme_size == 45, "Unexpected decoder query shape");
            anyhow::ensure!(phoneme.len() == length, "f0/phoneme frame count differs");

            // Full-range streaming synthesis passes all intermediate rows,
            // including MARGIN frames at each end, to the pinned vocoder.
            let n = length.checked_add(2 * super::MARGIN)
                .ok_or_else(|| anyhow::anyhow!("Vocoder length overflow"))?;
            let _ = i64::try_from(n)?;
            Ok(n)
        }

'''

CORE_FIXED_OPTION = r'''
        let benchmark_fixed_length =
            match (crate::__benchmark_fixed_shape::requested_length(), model) {
                (Some(n), ModelBytes::Onnx(bytes))
                    if bytes.len() == crate::__benchmark_fixed_shape::VOCODER_BYTES =>
                {
                    use sha2::{Digest as _, Sha256};
                    let digest: [u8; 32] = Sha256::digest(bytes).into();
                    (digest == crate::__benchmark_fixed_shape::VOCODER_SHA256).then_some(n)
                }
                _ => None,
            };
        if let Some(n) = benchmark_fixed_length {
            builder = builder
                .with_dimension_override("length", i64::try_from(n)?)
                .map_err(ort::Error::<()>::from)?
                .with_dimension_override("feats", 80)
                .map_err(ort::Error::<()>::from)?;
            if let Some(threads) = std::num::NonZeroUsize::new(crate::__benchmark_fixed_shape::xnn_threads()) {
                // Direct register calls the real C API and propagates errors;
                // the pinned binding's platform hint omits WASM.
                ort::ep::XNNPACK::default().with_intra_op_num_threads(threads).register(&mut builder)?;
            }
            if crate::__benchmark_fixed_shape::profiling_enabled() {
                builder = builder.with_profiling("bench-ep").map_err(ort::Error::<()>::from)?;
            }
        }

'''

CORE_FIXED_VERIFY = r'''
        if let Some(n) = benchmark_fixed_length {
            let n = i64::try_from(n)?;
            ensure!(sess.inputs().len() == 1, "Unexpected fixed vocoder input count");
            for (name, expected_type, expected_shape) in [
                ("spec", TensorElementType::Float32, vec![n, 80]),
            ] {
                let info = sess.inputs().iter()
                    .find(|input| input.name() == name)
                    .with_context(|| format!("Missing fixed vocoder input {name}"))?;
                let ValueType::Tensor { ty, shape, .. } = info.dtype() else {
                    bail!("Fixed vocoder input {name} is not a tensor");
                };
                ensure!(*ty == expected_type, "Unexpected fixed vocoder dtype for {name}");
                ensure!(
                    &shape[..] == expected_shape.as_slice(),
                    "Fixed vocoder shape mismatch for {}: expected {:?}, got {:?}",
                    name, expected_shape, shape
                );
            }
            ensure!(
                sess.outputs().len() == 1 && sess.outputs()[0].name() == "wave",
                "Unexpected fixed vocoder output"
            );
            let ValueType::Tensor { ty, .. } = sess.outputs()[0].dtype() else {
                bail!("Fixed vocoder output is not a tensor");
            };
            ensure!(*ty == TensorElementType::Float32, "Unexpected fixed vocoder output dtype");
            crate::__benchmark_fixed_shape::record_verified_session()?;
            if crate::__benchmark_fixed_shape::xnn_threads() > 0 {
                crate::__benchmark_fixed_shape::record_xnn_session()?;
            }
        }

'''


def patch_fixed_shape(source: Path) -> None:
    def insert(path: Path, marker: str, addition: str, token: str) -> None:
        content = path.read_text("utf-8")
        if token in content:
            if addition not in content or content.count(token) != 1:
                raise RuntimeError("Cached CORE fixed-shape patch differs from this script")
            return
        if content.count(marker) != 1:
            raise RuntimeError("Pinned CORE does not match a fixed-shape patch marker")
        path.write_text(content.replace(marker, addition + marker, 1), encoding="utf-8")
    crate = source / "crates/voicevox_core"
    manifest = crate / "Cargo.toml"
    content = manifest.read_text("utf-8")
    if "sha2.workspace = true" not in content:
        if content.count("[dependencies]\n") != 1 or re.search(r"^sha2\s*[.=]", content, re.M):
            raise RuntimeError("Unexpected CORE sha2 dependency declaration")
        manifest.write_text(content.replace("[dependencies]\n", "[dependencies]\nsha2.workspace = true\n", 1), encoding="utf-8")
    content = manifest.read_text("utf-8")
    ort_line = 'ort = { workspace = true, features = ["std", "ndarray", "tracing", "api-17", "alternative-backend"], default-features = false }'
    ort_xnn_line = ort_line.replace('"alternative-backend"', '"alternative-backend", "xnnpack"')
    if ort_xnn_line not in content:
        if content.count(ort_line) != 1:
            raise RuntimeError("Pinned CORE ort feature declaration differs")
        manifest.write_text(content.replace(ort_line, ort_xnn_line, 1), encoding="utf-8")
    library = crate / "src/lib.rs"
    content = library.read_text("utf-8")
    if "pub mod __benchmark_fixed_shape" not in content:
        library.write_text(content + "\n" + CORE_FIXED_CONFIG, encoding="utf-8")
    elif CORE_FIXED_CONFIG not in content:
        raise RuntimeError("Cached CORE fixed-shape configuration differs from this script")
    insert(crate / "src/synthesizer.rs", "        // Untimed diagnostic only: uses the pinned Talk/StreamingTalk domain dispatch\n",
           CORE_FIXED_HELPER, "pub fn benchmark_vocoder_length")
    runtime = crate / "src/core/infer/runtimes/onnxruntime.rs"
    insert(runtime, "        let sess = match model {\n", CORE_FIXED_OPTION, "let benchmark_fixed_length =")
    insert(runtime, "        let input_param_infos = sess\n", CORE_FIXED_VERIFY, "Unexpected fixed vocoder input count")


def write_wrapper(source: Path) -> None:
    synthesizer = source / "crates/voicevox_core/src/synthesizer.rs"
    contents = synthesizer.read_text("utf-8")
    if "pub fn benchmark_raw_synthesis" not in contents:
        marker = "        /// AudioQueryから直接WAVフォーマットで音声波形を生成する。\n"
        if contents.count(marker) != 1:
            raise RuntimeError("Pinned CORE synthesis source does not match the FP32 diagnostic patch")
        synthesizer.write_text(contents.replace(marker, CORE_BENCH_HELPER + marker, 1), encoding="utf-8")
    elif CORE_BENCH_HELPER not in contents:
        raise RuntimeError("Cached CORE FP32 diagnostic patch differs from this script")
    patch_fixed_shape(source)
    crate = source / "crates/voicevox_benchmark"
    (crate / "src").mkdir(parents=True, exist_ok=True)
    (crate / "Cargo.toml").write_text('''[package]
name = "voicevox_benchmark"
version = "0.0.0"
edition = "2024"
[features]
native = ["voicevox_core/load-onnxruntime"]
browser = ["voicevox_core/link-onnxruntime"]
threaded = []
[dependencies]
anyhow.workspace = true
serde_json.workspace = true
voicevox_core.workspace = true
ort.workspace = true
''', encoding="utf-8")
    (crate / "src/main.rs").write_text(RUST_SOURCE, encoding="utf-8")


def prepare_model(source: Path, destination: Path) -> None:
    import zipfile
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted((source / "model/sample.vvm").iterdir()):
            if path.is_file():
                info = zipfile.ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                with path.open("rb") as input_file, archive.open(info, "w") as output_file:
                    shutil.copyfileobj(input_file, output_file)


class NativeRunner:
    def __init__(self, executable: Path, runtime: Path, model: Path, query: Path, threads: int, style: int, wav: Path):
        self.wav = wav
        self.process = subprocess.Popen([str(executable), str(runtime), str(model), str(query), str(threads), str(style), str(wav)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        import queue
        import threading
        self._lines: queue.Queue[str | None] = queue.Queue()
        def read_lines() -> None:
            for line in self.process.stdout:
                self._lines.put(line)
            self._lines.put(None)
        self._reader = threading.Thread(target=read_lines, daemon=True)
        self._reader.start()
        try:
            message = self._read()
            if not message.get("ready") or message.get("threads") != threads:
                raise RuntimeError("Native initialization did not confirm its thread setting")
        except Exception:
            self.close()
            raise
        self.duration = 0.0
        self.bytes = 0

    def _read(self) -> dict[str, Any]:
        import queue
        try:
            line = self._lines.get(timeout=600)
        except queue.Empty:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("Native runner timed out after 10 minutes") from None
        if line is None:
            raise RuntimeError(f"Native runner exited unexpectedly ({self.process.poll()})")
        return json.loads(line)

    def synthesize(self, save: bool = False) -> tuple[float, float]:
        self.process.stdin.write("synthesize-save\n" if save else "synthesize\n")
        self.process.stdin.flush()
        message = self._read()
        if save:
            self.duration = wav_duration(self.wav.read_bytes())
            self.bytes = message["wav_bytes"]
        if not self.duration or message["wav_bytes"] != self.bytes:
            raise RuntimeError("Native WAV length changed between trials")
        return message["elapsed_s"], self.duration

    def raw_wave(self) -> bytes:
        self.process.stdin.write("raw-save\n")
        self.process.stdin.flush()
        message = self._read()
        data = self.wav.with_suffix(".f32").read_bytes()
        if len(data) != message["raw_bytes"]:
            raise RuntimeError("Native FP32 length mismatch")
        return data

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.process.stdin.write("quit\n")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait()
        self._reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class BrowserRunner:
    def __init__(self, browser: Any, url: str, module: str, threads: int, threaded: bool, style: int, wav: Path,
                 *, fixed_shape: bool = False, spin_off: bool = False, xnn_threads: int = 0, profile: bool = False):
        self.page = browser.new_page()
        self.page.set_default_timeout(600_000)
        self.page.goto(url, wait_until="load")
        message = self.page.evaluate("data => request(data)", {"command": "init", "module": module, "threads": threads, "threaded": threaded, "fixed_shape": fixed_shape, "spin_off": spin_off, "xnn_threads": xnn_threads, "profile": profile})
        if not message.get("ready") or message.get("threads") != threads or message.get("shared_memory") != threaded or message.get("spin_off") != spin_off:
            self.page.close()
            raise RuntimeError("Browser did not confirm the required thread/shared-memory configuration")
        self.style, self.wav, self.duration, self.bytes = style, wav, 0.0, 0
        self.shared_memory = message["shared_memory"]
        self.pthreads_created = message.get("pthreads_created", 0)
        self.spin_off = message["spin_off"]
        self.xnn_threads, self.xnn_sessions = message["xnn_threads"], message["xnn_sessions"]
        if self.xnn_threads != xnn_threads or self.xnn_sessions != int(xnn_threads > 0):
            self.page.close()
            raise RuntimeError("XNNPACK was not registered for exactly the requested vocoder session")
        self.fixed_length, self.fixed_matches = message["fixed_length"], message["fixed_matches"]
        if (fixed_shape and (self.fixed_length <= 0 or self.fixed_matches != 1)) or (not fixed_shape and (self.fixed_length != 0 or self.fixed_matches != 0)):
            self.page.close()
            raise RuntimeError("Fixed vocoder shape was not applied to exactly the requested session")

    def synthesize(self, save: bool = False) -> tuple[float, float]:
        message = self.page.evaluate("data => request(data)", {"command": "synthesize", "style": self.style, "save": save})
        if save:
            data = bytes(message["wav"])
            atomic_write(self.wav, data)
            self.duration = wav_duration(data)
            self.bytes = len(data)
        if not self.duration or message["wav_bytes"] != self.bytes:
            raise RuntimeError("Browser WAV length changed between trials")
        return message["elapsed_s"], self.duration

    def close(self) -> None:
        self.page.close()

    def raw_wave(self) -> bytes:
        message = self.page.evaluate("data => request(data)", {"command": "raw", "style": self.style})
        return bytes(message["raw"])

    def thread_state(self) -> int:
        return int(self.page.evaluate("() => request({command:'thread-state'})")["pthreads"])

    def finish_profile(self, profile_path: Path | None = None) -> dict[str, Any]:
        message = self.page.evaluate("() => request({command:'finish-profile'})")
        text = message["profile_text"]
        if text.count("BENCH_PROFILE_BEGIN") != 1 or text.count("BENCH_PROFILE_END") != 1:
            raise RuntimeError("Provider profile stdout markers are missing or ambiguous")
        events = json.loads(text.split("BENCH_PROFILE_BEGIN", 1)[1].split("BENCH_PROFILE_END", 1)[0].strip())
        if profile_path:
            atomic_write(profile_path, json.dumps(events, ensure_ascii=False, indent=2).encode())
        providers: dict[str, set[str]] = {}
        assignments: set[tuple[str, str, str]] = set()
        for event in events:
            if event.get("cat") == "Node" and event.get("name", "").endswith("_kernel_time") and event.get("args", {}).get("provider"):
                providers.setdefault(event["args"]["provider"], set()).add(event["name"])
                assignments.add((event["args"]["provider"], event["name"], event["args"].get("op_name", "")))
        return {"verified": bool(providers.get("XnnpackExecutionProvider")),
                "provider_kernel_counts": {key: len(nodes) for key, nodes in providers.items()},
                "provider_assignment_sha256": hashlib.sha256(json.dumps(sorted(assignments), separators=(",", ":")).encode()).hexdigest(),
                "profile_events": len(events), "profiling_scope": "separate untimed diagnostic browser only"}


def browser_engine_info(browser: Any) -> dict[str, Any]:
    session = browser.new_browser_cdp_session()
    try:
        version = session.send("Browser.getVersion")
        return {"product": version["product"], "js_version": version["jsVersion"]}
    finally:
        session.detach()


def revectorization_diagnostic_child(config_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    config = json.loads(config_path.read_text("utf-8"))
    with sync_playwright() as playwright:
        options = dict(config["options"])
        options["args"] = ["--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if config["enable"] else "--js-flags=--trace-wasm-revectorize"]
        browser = playwright.chromium.launch(**options)
        runner = None
        try:
            info = browser_engine_info(browser)
            info["requested_js_flags"] = options["args"]
            xnnpack = config.get("xnnpack", False)
            runner = BrowserRunner(browser, config["url"], "/xnnpack/voicevox_benchmark.js" if xnnpack else "/mt/voicevox_benchmark.js", config["threads"], True,
                                   config["style"], Path(config["wav"]), fixed_shape=xnnpack, spin_off=xnnpack,
                                   xnn_threads=config["threads"] if xnnpack else 0, profile=xnnpack)
            for _ in range(3):
                runner.synthesize(save=True)
            if xnnpack:
                pthreads = runner.thread_state()
                if pthreads != config["threads"] - 1:
                    raise RuntimeError("Combined diagnostic has unexpected thread-pool ownership")
                info["xnnpack_profile"] = {**runner.finish_profile(Path(config["profile_result"])),
                                           "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,
                                           "pthreads": pthreads, "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}
            atomic_write(Path(config["result"]), json.dumps(info).encode())
        finally:
            if runner:
                runner.close()
            browser.close()


def verify_revectorization(url: str, options: dict[str, Any], threads: int, style: int, work: Path,
                          trace_dir: Path | None = None, *, xnnpack: bool = False) -> dict[str, Any]:
    records = {}
    prefix = "combined" if xnnpack else "revec"
    for enable, key in ((False, "control"), (True, "candidate")):
        progress(f"Checking {prefix} {key} vector transformations in a separate untimed browser")
        config_path, result_path = work / f"{prefix}-{key}-config.json", work / f"{prefix}-{key}-result.json"
        atomic_write(config_path, json.dumps({"options": options, "url": url, "threads": threads, "style": style, "enable": enable,
                                            "xnnpack": xnnpack, "profile_result": str((trace_dir or work) / f"{key}-provider-profile.json"),
                                            "wav": str(work / f"{prefix}-{key}.wav"), "result": str(result_path)}).encode())
        env = dict(os.environ)
        env["DEBUG"] = "pw:browser"
        try:
            output = checked_run([sys.executable, str(Path(__file__).resolve()), "--diagnostic-config", str(config_path)], env=env)
        except RuntimeError as error:
            if trace_dir:
                atomic_write(trace_dir / f"{key}-failed-tail.log", str(error).encode())
            raise
        if trace_dir:
            atomic_write(trace_dir / f"{key}.log", output.encode())
        nodes = [int(value) for value in re.findall(r"Decided to vectorize, ([1-9][0-9]*) revectorizable nodes", output)]
        rejected = bool(re.search(r"(?:unrecognized|unknown|unrecognised|contradictory) (?:command[- ]line )?flags?|Error:.*(?:wasm-revectorize|trace-wasm-revectorize)", output, re.I))
        info = json.loads(result_path.read_text("utf-8"))
        expected = "--js-flags=--wasm-revectorize,--trace-wasm-revectorize" if enable else "--js-flags=--trace-wasm-revectorize"
        records[key] = {**info, "transformed_groups": len(nodes), "revectorizable_nodes": sum(nodes),
                        "flag_rejected": rejected, "launch_configuration_matches": info["requested_js_flags"] == [expected]}
    control, candidate = records["control"], records["candidate"]
    verified = (control["transformed_groups"] == 0 and candidate["transformed_groups"] > 0
                and all(value["launch_configuration_matches"] and not value["flag_rejected"] for value in records.values())
                and (control["product"], control["js_version"]) == (candidate["product"], candidate["js_version"]))
    if xnnpack:
        a, b = control["xnnpack_profile"], candidate["xnnpack_profile"]
        verified = verified and a["verified"] and b["verified"] and all(
            a[field] == b[field] for field in ("provider_assignment_sha256", "fixed_length", "fixed_matches", "pthreads", "xnn_threads", "xnn_sessions"))
    return {**records, "verified": verified,
            "evidence": "no transformations in trace-only control; positive nonempty V8 optimizer transformations with revectorization; actual CORE WASM, three untimed syntheses each; flags record supplied launch configuration, activation is established by traces"}


def verify_xnnpack(playwright: Any, url: str, options: dict[str, Any], threads: int, style: int, work: Path,
                   profile_path: Path | None = None) -> dict[str, Any]:
    progress("Checking XNNPACK kernel execution in a separate untimed profiled browser")
    browser = playwright.chromium.launch(**options)
    runner = None
    try:
        runner = BrowserRunner(browser, url, "/xnnpack/voicevox_benchmark.js", threads, True, style,
                               work / "xnnpack-diagnostic.wav", fixed_shape=True, spin_off=True, xnn_threads=threads, profile=True)
        runner.synthesize(save=True)
        pthreads = runner.thread_state()
        if pthreads != threads - 1:
            raise RuntimeError("XNNPACK process created an unexpected number of inference pthreads")
        return {**runner.finish_profile(profile_path), "pthreads_after_warmup": pthreads, "fixed_length": runner.fixed_length, "fixed_matches": runner.fixed_matches,
                "xnn_threads": runner.xnn_threads, "xnn_sessions": runner.xnn_sessions}
    finally:
        if runner:
            runner.close()
        browser.close()


@contextlib.contextmanager
def serve_assets(folder: Path) -> Iterator[str]:
    import functools
    import http.server
    import threading
    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()
        def log_message(self, *_: Any) -> None:
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

RUST_VERSION = "1.96.0"
EMSDK_VERSION = "4.0.8"
EMSDK_COMMIT = "419021fa040428bc69ef1559b325addb8e10211f"
BASE_RELEASE = "https://github.com/yamachu/onnxruntime-builder/releases/download/onnxruntime-1.23.2"
ORT_ARCHIVE_SHA256 = {'onnxruntime-linux-arm64-1.23.2.tgz': '121888dc9d8c6267f6373df150eed9cd2da5dfd4e277d99b22e092533790f61a', 'onnxruntime-linux-x64-1.23.2.tgz': '2e147a06354a4b75362d4a26e6c55b126d05e25dc19c609c1733aedb7156e8a2', 'onnxruntime-osx-arm64-1.23.2.tgz': 'a80514d3ecf04f8c7e8e8c2d0f1dd603bee0c383e84c1ca01e40bf7807c3d0ce', 'onnxruntime-osx-x86_64-1.23.2.tgz': '0c6489e161ea803e52e8153bdba4ea1541252c171776925f0f230091e1a5a949', 'onnxruntime-wasm-static-1.23.2.tgz': '2dc2c5073337b0e77e08314ca9936f5b9a355ff4e237c16294b969e7f2db6593', 'onnxruntime-win-arm64-1.23.2.tgz': '119b1e2fefde9b139c8a43d3e7ef1817b7ff8d551209916d5f1f8528dd451829', 'onnxruntime-win-x64-1.23.2.tgz': '9db1a87c4502e435319d24602ebd280c98d3d62f7c2b31f16e34de2143732bde'}
THREADED_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-1.23.2/onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"
XNNPACK_ORT_URL = "https://github.com/Hiroshiba/onnxruntime-builder/releases/download/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2/onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"
ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-xnnpack-1.23.2.tgz"] = "76182743b15a31e0d361432d8db297a78e3da64aad356730fe93576993af5a2a"
ORT_ARCHIVE_SHA256["onnxruntime-wasm-static-simd-threaded-1.23.2.tgz"] = "bdc024237b8303feb24c237bc7c8c07fdabd12a2bb7049684ed2894c580a63d1"


def unpack_cached(archive: Path, destination: Path) -> Path:
    import tarfile
    receipt = destination / ".complete.json"
    digest = sha256_file(archive)
    if receipt.is_file():
        try:
            if json.loads(receipt.read_text())["sha256"] == digest:
                return destination
        except (OSError, ValueError, KeyError):
            pass
    temporary = Path(tempfile.mkdtemp(prefix=".extract-", dir=destination.parent))
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter="data")
        atomic_write(temporary / ".complete.json", json.dumps({"sha256": digest}).encode())
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def find_one(folder: Path, name: str) -> Path:
    matches = [p for p in folder.rglob(name) if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {name}; found {len(matches)}")
    return matches[0].resolve()


def native_platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    arm = machine in {"aarch64", "arm64"}
    x64 = machine in {"x86_64", "amd64"}
    if not (arm or x64):
        raise RuntimeError(f"Unsupported architecture: {machine}; use 64-bit x86 or ARM")
    if sys.platform == "win32":
        return f"win-{'arm64' if arm else 'x64'}", f"{'aarch64' if arm else 'x86_64'}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"osx-{'arm64' if arm else 'x86_64'}", f"{'aarch64' if arm else 'x86_64'}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"linux-{'arm64' if arm else 'x64'}", f"{'aarch64' if arm else 'x86_64'}-unknown-linux-gnu"
    raise RuntimeError(f"Unsupported operating system: {sys.platform}")


def ensure_toolchains(root: Path) -> dict[str, str]:
    _, triple = native_platform()
    if sys.platform == "win32" and not shutil.which("cl.exe"):
        raise RuntimeError("Windows requires Visual Studio C++ Build Tools and an x64/ARM64 Native Tools command prompt. Install/activate those tools, then rerun the same uv command.")
    if sys.platform != "win32" and not (shutil.which("cc") and shutil.which("c++")):
        raise RuntimeError("A native C/C++ compiler is required. Install Xcode Command Line Tools on macOS, or your distribution's C/C++ build tools on Linux, then rerun.")
    import clang.cindex
    env = dict(os.environ)
    env["RUSTUP_HOME"] = str(root / "rustup")
    env["CARGO_HOME"] = str(root / "cargo")
    env["LIBCLANG_PATH"] = str(Path(clang.cindex.__file__).parent / "native")
    cargo_bin = root / "cargo/bin"
    env["PATH"] = str(cargo_bin) + os.pathsep + env["PATH"]
    suffix = ".exe" if sys.platform == "win32" else ""
    rustup = cargo_bin / ("rustup" + suffix)
    if not rustup.is_file():
        installer = cached_download(f"https://static.rust-lang.org/rustup/dist/{triple}/rustup-init{suffix}", root, "rustup-init" + suffix)
        if sys.platform != "win32":
            installer.chmod(0o755)
        checked_run([str(installer), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", RUST_VERSION, "--target", "wasm32-unknown-emscripten"], env=env)
    # rustup reuses installed components and repairs an interrupted installation.
    checked_run([str(rustup), "toolchain", "install", RUST_VERSION, "--profile", "minimal", "--target", "wasm32-unknown-emscripten", "--component", "rust-src"], env=env)
    emsdk_dir = root / f"emsdk-{EMSDK_VERSION}"
    archive = cached_download(f"https://github.com/emscripten-core/emsdk/archive/{EMSDK_COMMIT}.tar.gz", root, f"emsdk-{EMSDK_VERSION}.tar.gz")
    unpack_cached(archive, emsdk_dir)
    emsdk = find_one(emsdk_dir, "emsdk.py")
    installed = emsdk.parent / "upstream/emscripten"
    if not (installed / "emcc.py").is_file():
        checked_run([sys.executable, str(emsdk), "install", EMSDK_VERSION], env=env)
    checked_run([sys.executable, str(emsdk), "activate", EMSDK_VERSION], env=env)
    # Use the activated SDK's explicit paths without evaluating a shell script.
    node = find_one(emsdk.parent / "node", "node.exe" if sys.platform == "win32" else "node")
    env["EMSDK"] = str(emsdk.parent)
    env["EM_CONFIG"] = str(emsdk.parent / ".emscripten")
    env["EMSDK_NODE"] = str(node)
    env["EMSDK_PYTHON"] = sys.executable
    env["PATH"] = os.pathsep.join(map(str, [emsdk.parent, installed, node.parent])) + os.pathsep + env["PATH"]
    return env


def build_runner(source: Path, runtime: Path, root: Path, env: dict[str, str], *, threaded: bool | None, threads: int,
                 optimization: str = "z", graph_level: int = 1, xnnpack: bool = False) -> Path:
    if optimization not in ("z", "3") or graph_level not in (1, 3):
        raise ValueError("Unsupported benchmark optimization variant")
    write_wrapper(source)
    kind = "native" if threaded is None else ("browser-mt" if threaded else "browser-st")
    if optimization != "z":
        kind += "-o3"
    if graph_level != 1:
        kind += "-graph3"
    if xnnpack:
        kind += "-xnnpack"
    pool = 'Module["benchmarkPoolSize"]' if threaded else 0
    import inspect
    build_adapter = inspect.getsource(build_runner) + inspect.getsource(write_wrapper) + inspect.getsource(patch_fixed_shape)
    identity = {"build_adapter_sha256": hashlib.sha256(build_adapter.encode()).hexdigest(), "core": CORE_COMMIT, "rust": RUST_VERSION, "emscripten": EMSDK_VERSION,
                "runtime": sha256_file(runtime), "wrapper": hashlib.sha256(RUST_SOURCE.encode()).hexdigest(),
                "core_diagnostic_patch": hashlib.sha256(CORE_BENCH_HELPER.encode()).hexdigest(),
                "core_fixed_shape_patch": hashlib.sha256((CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY + "sha2.workspace = true; ort/xnnpack").encode()).hexdigest(),
                "kind": kind, "optimization": optimization, "graph_level": graph_level, "xnnpack": xnnpack,
                "pool": pool, "rebuild_std": bool(threaded), "platform": native_platform()[1]}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = root / "builds" / key
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    binary = folder / ("voicevox_benchmark" + (suffix if threaded is None else ".js"))
    receipt = folder / "complete.json"
    if binary.is_file() and receipt.is_file():
        info = json.loads(receipt.read_text())
        if info.get("identity") == identity and all((folder / name).is_file() and sha256_file(folder / name) == digest for name, digest in info["files"].items()):
            print(f"Cached build: {kind}", flush=True)
            return binary
    build_env = dict(env)
    # A caller's pre-existing optimization flags must not silently change this
    # experiment or reuse a binary built under a different precision policy.
    for name in list(build_env):
        if (name.startswith("CARGO_PROFILE_") or name in {"RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "EMCC_CFLAGS", "EMMAKEN_CFLAGS", "CARGO_BUILD_TARGET"}
                or name.startswith("CARGO_") and name.endswith("_RUSTFLAGS")
                or re.search(r"(^|_)(CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS)(_|$)", name)):
            build_env.pop(name)
    build_env["CARGO_PROFILE_C_API_OPT_LEVEL"] = optimization
    build_env["CARGO_TARGET_DIR"] = str(root / "cargo-target" / kind)
    cargo = str(root / "cargo/bin" / ("cargo.exe" if sys.platform == "win32" else "cargo"))
    command = [cargo, f"+{RUST_VERSION}", "build", "--manifest-path", str(source / "Cargo.toml"), "-p", "voicevox_benchmark", "--profile", "c-api"]
    if threaded is None:
        command += ["--features", "native"]
        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "c-api"
    else:
        command += ["--features", "browser,threaded" if threaded else "browser", "--target", "wasm32-unknown-emscripten"]
        features = "+simd128,+atomics,+bulk-memory,+mutable-globals" if threaded else "+simd128"
        flags = ["-C", f"target-feature={features}", "-C", "link-arg=-msimd128", "-C", "link-arg=-fwasm-exceptions", "-C", "link-arg=-fno-fast-math", "-C", "link-arg=-ffp-contract=off", "-C", "link-arg=-sALLOW_MEMORY_GROWTH=1",
                 "-C", "link-arg=-sINITIAL_MEMORY=1073741824", "-C", "link-arg=-sSTACK_SIZE=8388608",
                 "-C", "link-arg=-sEXPORTED_RUNTIME_METHODS=FS,HEAPU8" + (",PThread" if threaded else ""),
                 "-C", "link-arg=-sEXPORTED_FUNCTIONS=_main,_bench_init,_bench_synthesize,_bench_wav_ptr,_bench_wav_len,_bench_raw,_bench_raw_ptr,_bench_raw_len,_bench_spin_off,_bench_fixed_length,_bench_fixed_matches,_bench_xnn_threads,_bench_xnn_sessions,_bench_finish_profile",
                 "-L", f"native={runtime.parent}"]
        if optimization == "3":
            flags += ["-C", "link-arg=-O3"]
        if threaded:
            # The distributed wasm std has no atomics. Rebuild the matching 1.96
            # sources instead of mixing in a different nightly compiler.
            command += ["-Z", "build-std=std,panic_abort"]
            build_env["RUSTC_BOOTSTRAP"] = "1"
            flags += ["-C", "link-arg=-pthread", "-C", f"link-arg=-sPTHREAD_POOL_SIZE={pool}"]
            build_env["CFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"
            build_env["CXXFLAGS_wasm32_unknown_emscripten"] = "-pthread -msimd128"
        build_env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
        build_env["CARGO_TARGET_WASM32_UNKNOWN_EMSCRIPTEN_LINKER"] = "emcc.bat" if sys.platform == "win32" else "emcc"
        output_folder = Path(build_env["CARGO_TARGET_DIR"]) / "wasm32-unknown-emscripten/c-api"
    progress(f"Building {kind}: CORE opt-level={optimization}, ORT graph Level{graph_level} (first run may take several minutes)")
    graph_source = source / "crates/voicevox_core/src/core/infer/runtimes/onnxruntime.rs"
    original = graph_source.read_text("utf-8")
    pattern = ".with_optimization_level(GraphOptimizationLevel::Level1)"
    if original.count(pattern) != 1:
        raise RuntimeError("Pinned CORE graph optimization source does not match Level1")
    try:
        if graph_level == 3:
            graph_source.write_text(original.replace(pattern, ".with_optimization_level(GraphOptimizationLevel::Level3)"), encoding="utf-8")
        checked_run(command, env=build_env)
    finally:
        if graph_level == 3:
            graph_source.write_text(original, encoding="utf-8")
    generated = [output_folder / binary.name]
    if threaded is not None:
        generated += list(output_folder.glob("voicevox_benchmark.wasm")) + list(output_folder.glob("voicevox_benchmark.worker.js"))
        if not any(path.suffix == ".wasm" for path in generated):
            raise RuntimeError("Build did not produce WASM")
    hashes = {}
    for path in generated:
        shutil.copy2(path, folder / path.name)
        hashes[path.name] = sha256_file(folder / path.name)
    atomic_write(receipt, json.dumps({"identity": identity, "files": hashes}).encode())
    return binary


def verify_threaded_runtime(folder: Path, *, xnnpack: bool = False) -> dict[str, Any]:
    info_path = find_one(folder, "BUILD_INFO.json")
    info = json.loads(info_path.read_text("utf-8"))
    expected = {"schema_version": 1, "library": "onnxruntime", "version": ORT_VERSION,
                "source_commit": "a83fc4d58cb48eb68890dd689f94f28288cf2278",
                "emscripten_version": EMSDK_VERSION, "target": "wasm32-unknown-emscripten",
                "simd": True, "pthreads": True, "signed": False, "exception_abi": "wasm", "thread_pool_scope": "global"}
    if xnnpack:
        expected.update({"xnnpack": True, "relaxed_simd": False, "fast_math": False, "fp_contract": "off",
                         "thread_pool_scope": "global_ort_plus_per_session_xnnpack", "pthreadpool_backend": "pthreads",
                         "pthreadpool_execution_smoke_passed": True})
    if any(info.get(key) != value for key, value in expected.items()):
        raise RuntimeError("The threaded archive does not match the required generic ORT/SIMD/pthread build")
    if not info.get("smoke_test", {}).get("passed"):
        raise RuntimeError("The threaded runtime's build smoke test did not pass")
    if xnnpack and not all(info["smoke_test"].get(key) for key in ("profile_verified", "numerical_reference_verified")):
        raise RuntimeError("XNNPACK build lacks verified provider activity and numerical smoke checks")
    runtime = find_one(folder, "libonnxruntime_webassembly.a")
    sums = (info_path.parent / "SHA256SUMS").read_text("utf-8")
    entry = next((line.split()[0] for line in sums.splitlines() if line.split() and line.split()[-1].lstrip("*") == "lib/libonnxruntime_webassembly.a"), None)
    if entry != sha256_file(runtime):
        raise RuntimeError("Threaded runtime library checksum mismatch")
    return info


def verify_archive_sidecar(archive: Path) -> str:
    sidecar = archive.with_name(archive.name + ".sha256")
    entries = [line.split() for line in sidecar.read_text("utf-8").splitlines() if line.strip()]
    match = next((parts[0] for parts in entries if len(parts) == 2 and Path(parts[1].lstrip("*")).name == archive.name), None)
    if not match or not re.fullmatch("[a-fA-F0-9]{64}", match) or sha256_file(archive) != match.lower():
        raise RuntimeError("Threaded archive checksum mismatch")
    return match.lower()


def run_benchmark(args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright
    import psutil
    progress("Preparing benchmark inputs and cached tools")
    if sum(map(bool, (args.experiments, args.baseline_only, args.backend_experiments))) > 1:
        raise ValueError("Choose only one of --experiments, --baseline-only, or --backend-experiments")
    use_xnnpack = args.backend_experiments == "all"
    if use_xnnpack and args.style_id != 302:
        raise ValueError("XNNPACK experiments target sample.vvm streaming Style302; use --style-id 302")
    if args.experiments and args.experimental_threads > logical_cpu_count():
        progress(f"Warning: experimental threads={args.experimental_threads} exceeds available logical CPUs={logical_cpu_count()}")
    if not 0 <= args.style_id <= 2**32 - 1:
        raise ValueError("--style-id must be an unsigned 32-bit integer")
    query = json.loads(args.audio_query.read_text("utf-8")) if args.audio_query else prepared_query(args.target_seconds)
    if not isinstance(query, dict) or not isinstance(query.get("outputSamplingRate"), int) or query["outputSamplingRate"] <= 0:
        raise ValueError("AudioQuery must contain a positive integer outputSamplingRate")
    query_bytes = json.dumps(query, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    root = args.cache_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    threaded_url = args.threaded_ort_url or THREADED_ORT_URL
    xnnpack_url = args.xnnpack_ort_url or XNNPACK_ORT_URL
    if use_xnnpack and not xnnpack_url and not args.xnnpack_ort_archive:
        raise RuntimeError("The XNNPACK runtime is not configured until its validated release is available; no XNNPACK measurement was made")
    if not args.baseline_only and not threaded_url and not args.threaded_ort_archive:
        raise RuntimeError("The unsigned multithreaded ORT release is not yet configured. Supply its verified archive URL using --threaded-ort-url; single-thread results are not a substitute for this mode.")
    begin = time.monotonic()
    progress(f"Preparing Rust {RUST_VERSION} and Emscripten {EMSDK_VERSION}")
    env = ensure_toolchains(root)
    progress("Preparing CORE source and ONNX Runtime archives")
    core_archive = cached_download(f"https://github.com/yamachu/voicevox_core/archive/{CORE_COMMIT}.tar.gz", root, f"core-{CORE_COMMIT}.tar.gz")
    source_patch_key = hashlib.sha256((CORE_BENCH_HELPER + CORE_FIXED_CONFIG + CORE_FIXED_HELPER + CORE_FIXED_OPTION + CORE_FIXED_VERIFY).encode()).hexdigest()[:16]
    core_tree = unpack_cached(core_archive, root / f"source-{CORE_COMMIT}-{source_patch_key}")
    source = next(path.parent for path in core_tree.glob("*/Cargo.toml"))
    platform_name, _ = native_platform()
    runtime_archives = {}
    urls = {} if args.backend_experiments == "v8" else {
        "native": f"{BASE_RELEASE}/onnxruntime-{platform_name}-{ORT_VERSION}.tgz",
        "browser": f"{BASE_RELEASE}/onnxruntime-wasm-static-{ORT_VERSION}.tgz",
    }
    if not args.baseline_only:
        urls["browser_mt"] = threaded_url
    if use_xnnpack:
        urls["browser_xnnpack"] = xnnpack_url or "local-xnnpack-archive"
    for name, url in urls.items():
        local_archive = args.threaded_ort_archive if name == "browser_mt" else (args.xnnpack_ort_archive if name == "browser_xnnpack" else None)
        if local_archive:
            archive = local_archive.resolve()
            verify_archive_sidecar(archive)
        else:
            from urllib.parse import urlparse
            filename = Path(urlparse(url).path).name
            archive = cached_download(url, root, filename, expected_sha256=ORT_ARCHIVE_SHA256.get(filename))
        (root / "runtimes").mkdir(exist_ok=True)
        extracted = unpack_cached(archive, root / "runtimes" / (name + "-" + sha256_file(archive)[:16]))
        runtime_archives[name] = (archive, extracted)
    library_name = "onnxruntime.dll" if sys.platform == "win32" else (f"libonnxruntime.{ORT_VERSION}.dylib" if sys.platform == "darwin" else f"libonnxruntime.so.{ORT_VERSION}")
    threaded_info = verify_threaded_runtime(runtime_archives["browser_mt"][1]) if not args.baseline_only else None
    xnnpack_info = verify_threaded_runtime(runtime_archives["browser_xnnpack"][1], xnnpack=True) if use_xnnpack else None
    native_runtime = find_one(runtime_archives["native"][1], library_name) if "native" in runtime_archives else None
    st_runtime = find_one(runtime_archives["browser"][1], "libonnxruntime_webassembly.a") if "browser" in runtime_archives else None
    mt_runtime = find_one(runtime_archives["browser_mt"][1], "libonnxruntime_webassembly.a") if not args.baseline_only else None
    native_binary = build_runner(source, native_runtime, root, env, threaded=None, threads=args.threads) if native_runtime else None
    browser_st = build_runner(source, st_runtime, root, env, threaded=False, threads=1) if st_runtime else None
    browser_mt = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads) if mt_runtime else None
    browser_binaries = {"st": browser_st} if browser_st else {}
    if browser_mt:
        browser_binaries["mt"] = browser_mt
    if use_xnnpack:
        xnn_runtime = find_one(runtime_archives["browser_xnnpack"][1], "libonnxruntime_webassembly.a")
        browser_binaries["xnnpack"] = build_runner(source, xnn_runtime, root, env, threaded=True, threads=args.threads, xnnpack=True)
    if args.experiments:
        browser_binaries["o3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, optimization="3")
        browser_binaries["graph3"] = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads, graph_level=3)
    model = root / f"sample-v1-{CORE_COMMIT}.vvm"
    if not model.exists():
        descriptor, temporary_name = tempfile.mkstemp(prefix=".sample-", dir=root)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            prepare_model(source, temporary)
            os.replace(temporary, model)
        finally:
            temporary.unlink(missing_ok=True)
    modes = [] if args.backend_experiments == "v8" else [Mode("native", f"Native ×{args.threads}", args.threads, "native CPU, per-session thread pools"),
             Mode("browser", "Browser ×1", 1, "WebAssembly SIMD, unshared memory")]
    if not args.baseline_only:
        modes.append(Mode("browser_mt", f"Browser pthreads ×{args.threads}", args.threads, "WebAssembly SIMD + pthreads, global thread pool"))
    if args.experiments:
        modes.extend([
            Mode("browser_mt_threads", f"実験 MT ×{args.experimental_threads}", args.experimental_threads, "experimental: thread count only; global pool", experimental=True),
            Mode("browser_mt_o3", f"実験 MT ×{args.threads} O3", args.threads, "experimental: CORE opt-level=3 + final Emscripten -O3; existing LTO retained", core_optimization="3", experimental=True),
            Mode("browser_mt_graph3", f"実験 MT ×{args.threads} Graph L3", args.threads, "experimental: ORT GraphOptimizationLevel::Level3 (ORT_ENABLE_LAYOUT)", graph_optimization=3, experimental=True),
        ])
    if use_xnnpack:
        modes.extend([
            Mode("browser_mt_fixed", f"CPU ×{args.threads} fixed shape", args.threads, "matching CPU control for XNNPACK: query-derived vocoder-only fixed length", fixed_shape=True),
            Mode("browser_xnnpack", f"実験 XNNPACK ×{args.threads}", args.threads, "XNNPACK vocoder-only pool; ORT global fallback intra=1/inter=1, spin disabled; compare with fixed-shape CPU control", experimental=True, fixed_shape=True, spin_off=True, execution_provider="XNNPACK"),
        ])
    if args.backend_experiments:
        modes.append(Mode("browser_revectorize", f"実験 V8 revectorize ×{args.threads}", args.threads, "same dynamic-shape CPU WASM as MT control; only --js-flags=--wasm-revectorize", experimental=True, revectorize=True))
    if use_xnnpack:
        modes.append(Mode("browser_xnnpack_revectorize", f"実験 XNNPACK + V8 ×{args.threads}", args.threads,
                          "same fixed-shape XNNPACK WASM and pools; add only --js-flags=--wasm-revectorize", experimental=True,
                          fixed_shape=True, spin_off=True, execution_provider="XNNPACK", revectorize=True))
    log: list[dict[str, Any]] = [{"event": "assets_ready", "seconds": round(time.monotonic() - begin, 3)}]
    environment = environment_info()
    environment["requested_backend_experiments"] = args.backend_experiments or "none"
    environment.update({"cpu_metric": "sum target-tree user+system CPU seconds between action boundary snapshots / action snapshot seconds; excludes pre/post padding; 100%=one logical CPU", "cpu_sample_interval_ms": CPU_SAMPLE_INTERVAL_S * 1000,
                        "cpu_padding_seconds": CPU_PADDING_S,
                        "cpu_trace_clock": "actual snapshot-completion times relative to synthesis request start; synchronized pre/action/post boundary snapshots; no internal operator-phase attribution",
                        "cpu_plot_aggregation": "common 0.25s request-relative pre/action bins and separate response-end-relative post bins; overlap-weighted rates per phase-complete trial, then median/inclusive quartiles; no zero padding",
                        "cpu_browser_scope": "separate Chromium instance per mode; root and descendants; Python/Playwright Node excluded", "psutil": psutil.__version__})
    environment.update({"rust": checked_run([str(root / "cargo/bin" / ("rustc.exe" if sys.platform == "win32" else "rustc")), f"+{RUST_VERSION}", "--version"], env=env),
                        "emscripten": EMSDK_VERSION, "sample_vvm_sha256": sha256_file(model),
                        "audio_query_sha256": hashlib.sha256(query_bytes).hexdigest(),
                        "browser_thread_support": "single-threaded baseline" if args.baseline_only else "shared WASM memory + global ORT pool verified at initialization"})
    if threaded_info:
        environment.update({"threaded_builder_commit": threaded_info["builder_commit"],
                            "threaded_validation_commit": threaded_info["validation_commit"],
                            "threaded_validation_run_id": threaded_info["validation_run_id"],
                            "threaded_rust_std": "Rust 1.96.0 rebuilt with atomics (build-std)",
                            "threaded_pthread_pool_size": args.threads,
                            "browser_mt_thread_pool": "global; intra-op=requested threads, inter-op=1"})
    for name, (archive, _) in runtime_archives.items():
        environment[f"{name}_ort_archive_sha256"] = sha256_file(archive)
    environment["core_variants"] = {mode.key: {"opt_level": mode.core_optimization, "graph_level": mode.graph_optimization,
                                               "threads": mode.threads, "experimental": mode.experimental,
                                               "fixed_shape": mode.fixed_shape, "global_spin_off": mode.spin_off,
                                               "execution_provider": mode.execution_provider, "revectorize": mode.revectorize} for mode in modes}
    if xnnpack_info:
        environment["xnnpack_builder_commit"] = xnnpack_info["builder_commit"]
        environment["xnnpack_thread_ownership"] = f"XNNPACK intra={args.threads}; ORT global intra=1/inter=1; only one SHA-matched vocoder session registers XNNPACK"
    environment["wasm_bytes"] = {key: binary.with_suffix(".wasm").stat().st_size for key, binary in browser_binaries.items()}
    environment["precision_flags"] = "FP32 model; unchanged strict FP/SIMD flags; no fast-math, relaxed SIMD, FP16 or quantization"
    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)]
    # Browser cache is scoped to this script, including on a second run.
    previous_browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "playwright")
    try:
        if not args.browser_path:
            progress("Preparing cached Chromium")
            checked_run([sys.executable, "-m", "playwright", "install", "chromium"], env=dict(os.environ))
        with tempfile.TemporaryDirectory(prefix="voicevox-benchmark-") as temporary:
            work = Path(temporary)
            # Only explicit benchmark assets are reachable from the loopback server.
            shutil.copyfile(model, work / "sample.vvm")
            atomic_write(work / "query.json", query_bytes)
            atomic_write(work / "index.html", BROWSER_PAGE.encode())
            atomic_write(work / "worker.js", BROWSER_WORKER.encode())
            for mode_key, binary in browser_binaries.items():
                (work / mode_key).mkdir()
                for path in binary.parent.glob("voicevox_benchmark.*"):
                    if path.suffix in {".js", ".wasm"}:
                        shutil.copy2(path, work / mode_key / path.name)
            with serve_assets(work) as url, sync_playwright() as playwright:
                options: dict[str, Any] = {"headless": not args.headed}
                if args.browser_path:
                    options["executable_path"] = str(args.browser_path)
                environment["browser_headless"] = not args.headed
                skipped: dict[str, str] = {}
                if args.backend_experiments:
                    environment["xnnpack_diagnostic"] = {"requested": use_xnnpack, "verified": False}
                    environment["combined_diagnostic"] = {"requested": use_xnnpack, "verified": False}
                    if use_xnnpack:
                        try:
                            environment["xnnpack_diagnostic"] = verify_xnnpack(playwright, url, options, args.threads, args.style_id, work,
                                                                            args.output.parent / "xnnpack-diagnostic-profile.json")
                        except Exception as error:
                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                            environment["xnnpack_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    try:
                        environment["revectorization_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,
                                                                                          args.output.parent / "v8-diagnostic-traces")
                    except Exception as error:
                        reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                        environment["revectorization_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    if use_xnnpack and environment["xnnpack_diagnostic"]["verified"]:
                        try:
                            environment["combined_diagnostic"] = verify_revectorization(url, options, args.threads, args.style_id, work,
                                                                                       args.output.parent / "combined-diagnostic-traces", xnnpack=True)
                        except Exception as error:
                            reason = str(error).replace(str(work), "<temporary>").replace(str(root), "<cache>").replace(str(Path(__file__).resolve()), "<script>").replace(str(Path.home()), "<home>")
                            environment["combined_diagnostic"] = {"verified": False, "error": reason[-1500:]}
                    if use_xnnpack and not environment["xnnpack_diagnostic"]["verified"]:
                        skipped["browser_xnnpack"] = "XNNPACKは実行プロファイルで対象カーネルを確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key not in {"browser_xnnpack", "browser_mt_fixed", "browser_xnnpack_revectorize"}]
                    if not environment["revectorization_diagnostic"]["verified"]:
                        skipped["browser_revectorize"] = "V8再ベクトル化は基準との差を示す変換ログを確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key != "browser_revectorize"]
                    if use_xnnpack and not environment["combined_diagnostic"]["verified"]:
                        skipped["browser_xnnpack_revectorize"] = "XNNPACKとV8の併用は同じ演算割当と実際のV8変換を確認できず、未測定。"
                        modes = [mode for mode in modes if mode.key != "browser_xnnpack_revectorize"]
                    environment["skipped_candidates"] = skipped
                    requested = environment["core_variants"]
                    measured_keys = {mode.key for mode in modes}
                    if skipped:
                        environment["requested_but_unmeasured"] = {key: value for key, value in requested.items() if key not in measured_keys}
                    environment["core_variants"] = {key: value for key, value in requested.items() if key in measured_keys}
                    atomic_write(args.output.with_suffix(".diagnostics.json"), json.dumps({key: environment[key] for key in ("xnnpack_diagnostic", "revectorization_diagnostic", "combined_diagnostic", "skipped_candidates")}, ensure_ascii=False, indent=2).encode())
                    if len(modes) < 2:
                        raise RuntimeError("Neither requested backend passed activation diagnostics; diagnostics saved, no purported optimized timings measured")
                    environment["block_order"] = [block.mode for block in make_schedule(modes, args.seed, balanced=True)]
                    progress("Balanced block order: " + " → ".join(environment["block_order"]))
                runners: dict[str, Any] = {}
                browsers: dict[str, Any] = {}
                cpu_roots: dict[str, int] = {}
                try:
                    if native_binary:
                        progress("Initializing native CORE")
                        started = time.monotonic()
                        runners["native"] = NativeRunner(native_binary, native_runtime, model, work / "query.json", args.threads, args.style_id, work / "native.wav")
                        cpu_roots["native"] = runners["native"].process.pid
                        log.append({"event": "initialized", "mode": "native", "seconds": round(time.monotonic() - started, 3)})
                    browser_modes = [("browser", "/st/voicevox_benchmark.js", 1, False)] if browser_st else []
                    if browser_mt:
                        browser_modes.append(("browser_mt", "/mt/voicevox_benchmark.js", args.threads, True))
                    if args.experiments:
                        browser_modes.extend([
                            ("browser_mt_threads", "/mt/voicevox_benchmark.js", args.experimental_threads, True),
                            ("browser_mt_o3", "/o3/voicevox_benchmark.js", args.threads, True),
                            ("browser_mt_graph3", "/graph3/voicevox_benchmark.js", args.threads, True),
                        ])
                    if args.backend_experiments:
                        browser_modes.extend([
                            ("browser_mt_fixed", "/mt/voicevox_benchmark.js", args.threads, True),
                            ("browser_xnnpack", "/xnnpack/voicevox_benchmark.js", args.threads, True),
                            ("browser_revectorize", "/mt/voicevox_benchmark.js", args.threads, True),
                            ("browser_xnnpack_revectorize", "/xnnpack/voicevox_benchmark.js", args.threads, True),
                        ])
                        browser_modes = [row for row in browser_modes if any(mode.key == row[0] for mode in modes)]
                    for key, module, threads, threaded in browser_modes:
                        progress(f"Initializing isolated {key} Chromium and CORE")
                        started = time.monotonic()
                        configured = next(mode for mode in modes if mode.key == key)
                        launch_options = dict(options)
                        if configured.revectorize:
                            launch_options["args"] = ["--js-flags=--wasm-revectorize"]
                        browser = playwright.chromium.launch(**launch_options)
                        browsers[key] = browser
                        cpu_roots[key] = chromium_process_id(browser)
                        if "browser" in environment and environment["browser"] != browser.version:
                            raise RuntimeError("Browser versions differ between modes")
                        environment["browser"] = browser.version
                        runners[key] = BrowserRunner(browser, url, module, threads, threaded, args.style_id, work / f"{key}.wav",
                                                     fixed_shape=configured.fixed_shape, spin_off=configured.spin_off,
                                                     xnn_threads=threads if configured.execution_provider == "XNNPACK" else 0)
                        if configured.revectorize:
                            info = browser_engine_info(browser)
                            info["requested_js_flags"] = launch_options["args"]
                            if info["requested_js_flags"] != ["--js-flags=--wasm-revectorize"]:
                                raise RuntimeError("Timed launch must use only the revectorization flag without trace flags")
                            combined = configured.execution_provider == "XNNPACK"
                            checked_engine = environment["combined_diagnostic" if combined else "revectorization_diagnostic"]["candidate"]
                            if (info["product"], info["js_version"]) != (checked_engine["product"], checked_engine["js_version"]):
                                raise RuntimeError("Timed V8 version differs from its activation diagnostic")
                            environment["timed_combined_engine" if combined else "timed_revectorization_engine"] = info
                        environment[f"{key}_pthreads_created"] = runners[key].pthreads_created
                        environment[f"{key}_global_spin_off"] = runners[key].spin_off
                        if configured.execution_provider == "XNNPACK":
                            environment[f"{key}_xnn_threads"] = runners[key].xnn_threads
                            environment[f"{key}_xnn_sessions"] = runners[key].xnn_sessions
                            environment[f"{key}_ort_global_threads"] = 1
                        if configured.fixed_shape:
                            environment[f"{key}_fixed_length"] = runners[key].fixed_length
                            environment[f"{key}_fixed_matches"] = runners[key].fixed_matches
                            environment[f"{key}_vocoder_sha256"] = VOCODER_MODEL_SHA256
                            environment[f"{key}_verified_input_shapes"] = {"spec": [runners[key].fixed_length, 80]}
                        log.append({"event": "initialized", "mode": key, "seconds": round(time.monotonic() - started, 3)})
                    if "browser_mt" in runners:
                        environment["browser_mt_pthreads_created"] = runners["browser_mt"].pthreads_created
                    environment.update(runners["browser_mt" if args.backend_experiments else "browser"].page.evaluate("() => ({browser_hardware_concurrency:navigator.hardwareConcurrency,cross_origin_isolated:crossOriginIsolated,user_agent:navigator.userAgent})"))
                    for mode in modes:
                        progress(f"Warmup: {mode.label} (excluded from latency and CPU measurements)")
                        elapsed, duration = runners[mode.key].synthesize(save=True)
                        if isinstance(runners[mode.key], BrowserRunner):
                            pthreads = runners[mode.key].thread_state()
                            environment[f"{mode.key}_pthreads_after_warmup"] = pthreads
                            if mode.execution_provider == "XNNPACK" and pthreads != mode.threads - 1:
                                raise RuntimeError("Timed XNNPACK process has unexpected thread-pool ownership")
                        if mode.fixed_shape:
                            environment[f"{mode.key}_warmup_with_ort_shape_validation"] = True
                        log.append({"event": "warmup_excluded", "mode": mode.key, "seconds": elapsed, "audio_s": duration})
                    durations = [runner.duration for runner in runners.values()]
                    if max(durations) - min(durations) > 1 / query["outputSamplingRate"]:
                        raise RuntimeError("Native/browser output lengths differ; refusing a misleading comparison")
                    output_checks = verify_outputs(runners, "browser_mt" if browser_mt else "browser", log)
                    progress(f"Starting {len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK} serial trials with target-process CPU sampling")
                    def measured_synthesis(mode: Mode) -> tuple[float, float, CpuMeasurement]:
                        sampler = ProcessCpuSampler(cpu_roots[mode.key], mode.label)
                        return sampler.measure(runners[mode.key].synthesize)
                    trials = run_schedule(modes, args.seed, measured_synthesis, log, balanced=args.backend_experiments)
                    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials],
                              "environment": environment, "audio_s": durations[0], "style_id": args.style_id, "seed": args.seed, "log": log,
                              "output_checks": output_checks,
                              "schedule_method": "seeded position-balanced and pair-order-balanced design; randomized labels and round order" if args.backend_experiments else "seeded independent shuffle in each round",
                              "schedule": [asdict(block) for block in make_schedule(modes, args.seed, balanced=args.backend_experiments)],
                              "notes": ["出力検証は測定外。同じ実行のブラウザMT基準とPCM・PCM化前FP32を照合。非一致を精度維持とは判定しない。" if browser_mt else "出力検証は測定外。ブラウザST基準とPCM・PCM化前FP32を照合。"]}
                    if args.baseline_only:
                        result["notes"].append("この先行検証はnativeとブラウザ単一スレッドのみ。マルチスレッドは未実施。")
                    if args.backend_experiments:
                        if use_xnnpack:
                            result["notes"].append("XNNPACKの速度は同じ固定shapeのCPU対照と比較。")
                            result["notes"].append("併用の追加効果は同じXNNPACK単独と、全体の効果は固定shapeのCPU対照と比較。FP32形式の維持と数値の完全一致は別々に確認。")
                        else:
                            result["notes"].append("この実行はV8候補のみ。XNNPACKは測定対象外。")
                        result["notes"].append("V8再ベクトル化は同じ通常MT WASMとの比較。診断用プロファイル・トレースは時間測定とは別のブラウザで実行。")
                        result["notes"].extend(skipped.values())
                    validate_results(result)
                    progress("Writing measured HTML and raw JSON")
                    render_report(result, args.output)
                    atomic_write(args.output.with_suffix(".json"), json.dumps(result, ensure_ascii=False, indent=2).encode())
                    print(f"Report: {args.output.resolve()}")
                finally:
                    for runner in runners.values():
                        with contextlib.suppress(Exception):
                            runner.close()
                    for browser in browsers.values():
                        with contextlib.suppress(Exception):
                            browser.close()
    finally:
        if previous_browser_path is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous_browser_path



def positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threads", type=positive_int, default=default_threads(), help="native + multithreaded-browser inference threads; default: max(1, available logical CPUs // 2)")
    parser.add_argument("--seed", type=int, default=20261007, help="reproducible block order")
    parser.add_argument("--audio-query", type=Path, help="optional prepared AudioQuery JSON; default: built-in approximately 10-second query")
    parser.add_argument("--style-id", type=int, default=302)
    parser.add_argument("--target-seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, default=Path("voicevox-benchmark.html"))
    parser.add_argument("--cache-dir", type=Path, default=cache_root())
    parser.add_argument("--report-from", type=Path, help="regenerate HTML from a completed measurement JSON")
    parser.add_argument("--baseline-only", action="store_true", help="explicitly measure only native and browser single-thread (MT is not measured)")
    parser.add_argument("--experiments", action="store_true", help="add isolated MT thread-count, CORE O3, and ORT graph-Level3 candidates (90 trials total)")
    parser.add_argument("--backend-experiments", nargs="?", const="all", choices=("all", "v8"), help="all: MT/fixed-shape controls, XNNPACK and V8; v8: only MT control and V8; balanced blocks")
    parser.add_argument("--experimental-threads", type=positive_int, default=4, help="thread-count candidate used with --experiments; default: 4")
    parser.add_argument("--threaded-ort-archive", type=Path, help="local verified MT archive with adjacent .sha256 sidecar")
    parser.add_argument("--threaded-ort-url", help="verified unsigned threaded ORT archive from the fork release")
    parser.add_argument("--xnnpack-ort-archive", type=Path, help="local verified XNNPACK archive with adjacent .sha256 sidecar")
    parser.add_argument("--xnnpack-ort-url", help="verified strict-FP XNNPACK runtime archive from the fork release")
    parser.add_argument("--browser-path", type=Path, help="optional installed Chromium executable")
    parser.add_argument("--headed", action="store_true", help="show the otherwise identical browser runner")
    parser.add_argument("--self-test", action="store_true", help="check scheduling, validation and reporting without synthesizing")
    parser.add_argument("--diagnostic-config", type=Path, help=argparse.SUPPRESS)
    return parser


def self_test() -> None:
    progress("Self-test fixtures only; no real synthesis or CPU measurements")
    modes = [Mode("native", "Native", 2, "native CPU"), Mode("browser", "Browser", 1, "WebAssembly SIMD")]
    a = make_schedule(modes, 42)
    assert a == make_schedule(modes, 42)
    assert len(a) == 6 and all(sum(block.mode == mode.key for block in a) == 3 for mode in modes)
    assert max(1, logical_cpu_count() // 2) == default_threads()
    for count in (2, 3, 4, 5, 6, 7):
        candidates = [Mode(f"mode{i}", str(i), 2, "test") for i in range(count)]
        for seed in range(100):
            blocks = make_schedule(candidates, seed, balanced=True)
            rounds = [[block.mode for block in blocks[start:start + count]] for start in range(0, len(blocks), count)]
            for mode in candidates:
                positions = [row.index(mode.key) for row in rounds]
                counts = [positions.count(index) for index in range(count)]
                assert max(counts) - min(counts) <= 1
            for first in candidates:
                for second in candidates:
                    if first.key != second.key:
                        assert {row.index(first.key) < row.index(second.key) for row in rounds} == {False, True}
    # Explicit test fixture, confined to a temporary directory and never delivered
    # as benchmark measurements.
    log: list[dict[str, Any]] = []
    def fixture(mode: Mode) -> tuple[float, float, CpuMeasurement]:
        trace = (CpuInterval(-1.001, -0.001, 0.0, 0.0, 1, False, "pre"),
                 CpuInterval(-0.001, 0.119, 0.06 * mode.threads, 50.0 * mode.threads, 1, False),
                 CpuInterval(0.119, 0.199, 0.04 * mode.threads, 50.0 * mode.threads, 1, False),
                 CpuInterval(0.199, 1.199, 0.0, 0.0, 1, False, "post"))
        return 0.1 * mode.threads, 10.0, CpuMeasurement(0.1 * mode.threads, 0.2, 0.5 * mode.threads,
                                                     50.0 * mode.threads, 5, 1, False, trace)
    trials = run_schedule(modes, 42, fixture, log)
    result = {"schema_version": SCHEMA_VERSION, "created_at": "SELF-TEST FIXTURE", "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials], "environment": {"test": "<>&"}, "audio_s": 10.0, "style_id": 302, "seed": 42, "log": log}
    validate_results(result)
    assert len(list(csv.DictReader(io.StringIO(trial_csv(result["trials"]))))) == 30
    assert len(list(csv.DictReader(io.StringIO(cpu_trace_csv(result["trials"]))))) == 120
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "test.html"
        render_report(result, report)
        content = report.read_text()
        assert '<details><summary>生データ（CSV）</summary>' in content
        assert '<details open' not in content
        assert '<summary>CPU 時系列（CSV）</summary>' in content
        assert 'class="cpu-grid"' in content
        assert '&lt;&gt;&amp;' in content
        assert 'https://' not in content  # Standalone report requires no CDN.
        first = Path(folder) / "atomic.txt"
        atomic_write(first, b"first")
        atomic_write(first, b"second")
        assert first.read_bytes() == b"second"
    corrupt = json.loads(json.dumps(result))
    corrupt["trials"][0]["rtf"] = 999.0
    try:
        validate_results(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("Inconsistent RTF accepted")
    print("Self-test passed. No benchmark measurements were made.")


def main() -> None:
    args = argument_parser().parse_args()
    if args.diagnostic_config:
        revectorization_diagnostic_child(args.diagnostic_config)
        return
    if args.output.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("--output must end with .html or .htm")
    if args.self_test:
        self_test()
        return
    if args.report_from:
        result = json.loads(args.report_from.read_text("utf-8"))
        render_report(result, args.output)
        print(f"Report: {args.output.resolve()}")
        return
    run_benchmark(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Interrupted. A completed report was not written.")
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(f"Error: {error}") from error

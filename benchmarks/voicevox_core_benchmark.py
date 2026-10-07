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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

CORE_COMMIT = "9b539761f3e152b966e08c2de0784129fe8cf68d"
ORT_BUILDER_COMMIT = "117593885cd2a66e9cf17b4059e6424d7ea528c9"
ORT_VERSION = "1.23.2"
TRIALS_PER_BLOCK = 5
BLOCKS_PER_MODE = 3
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    threads: int
    backend: str
    core_optimization: str = "z"
    graph_optimization: int = 1
    experimental: bool = False


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
                        if identity[1] < self.started_epoch:
                            # Existing process first seen after the initial snapshot:
                            # do not attribute its earlier lifetime CPU to this trial.
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
                                                  100 * delta / span, len(self.processes), self.incomplete))
            self.previous_sample_wall = sampled
            self.samples += 1
        except Exception as error:
            self.error = error
            self.stop.set()

    def measure(self, action: Callable[[], tuple[float, float]]) -> tuple[float, float, CpuMeasurement]:
        import threading
        self.started_wall = time.perf_counter()
        self.started_epoch = time.time()
        self._sample(initial=True)
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        def sample_loop() -> None:
            last_heartbeat = time.perf_counter()
            while not self.stop.wait(self.interval):
                self._sample()
                now = time.perf_counter()
                if now - last_heartbeat >= 30:
                    progress(f"{self.label}: synthesis still running ({now - self.started_wall:.0f}s)")
                    last_heartbeat = now
        worker = threading.Thread(target=sample_loop, name="benchmark-cpu-sampler", daemon=True)
        worker.start()
        request_started = time.perf_counter()
        try:
            elapsed, duration = action()
        finally:
            self.stop.set()
            worker.join(timeout=5)
            if worker.is_alive():
                self.error = RuntimeError("CPU sampler did not stop")
            elif self.error is None:
                self._sample()
            window = self.previous_sample_wall - self.baseline_wall
        if self.error:
            raise RuntimeError(f"CPU sampling failed for {self.label}: {self.error}") from self.error
        cores = self.total / window
        trace = tuple(CpuInterval(item.start_s - request_started, item.end_s - request_started,
                                  item.cpu_time_s, item.cpu_percent, item.cpu_processes, item.cpu_incomplete)
                      for item in self.intervals)
        return elapsed, duration, CpuMeasurement(self.total, window, cores, 100 * cores,
                                                self.samples, len(self.processes), self.incomplete, trace)


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


def make_schedule(modes: list[Mode], seed: int) -> list[Block]:
    """Three shuffled rounds, each containing one five-trial block per mode."""
    if not modes or len({mode.key for mode in modes}) != len(modes):
        raise ValueError("Mode keys must be nonempty and unique")
    rng = random.Random(seed)
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
    for key, runner in runners.items():
        raw = reference_raw if key == reference else runner.raw_wave()
        comparisons[key] = waveform_comparison(reference_wav, reference_raw, runner.wav.read_bytes(), raw)
        progress(f"Output {key}: PCM {'exact' if comparisons[key]['pcm_exact'] else 'DIFFERS'}, FP32 {'exact' if comparisons[key]['fp32_exact'] else 'DIFFERS'}")
    result = {"reference_mode": reference, "reference_deterministic": repeated["pcm_exact"] and repeated["fp32_exact"],
              "reference_repeat": repeated, "modes": comparisons}
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
            writer.writerow({**{key: trial[key] for key in context}, "interval": index, **point})
    return stream.getvalue()


def validate_results(result: dict[str, Any]) -> None:
    if result.get("schema_version") not in (2, SCHEMA_VERSION):
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
        for check in [checks["reference_repeat"], *checks["modes"].values()]:
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
            for point in trace:
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
            if incomplete != trial["cpu_incomplete"]:
                raise ValueError("CPU interval coverage does not match its trial")
            if not math.isclose(sum(point["cpu_time_s"] for point in trace), trial["cpu_time_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("CPU interval counters do not sum to trial CPU time")
            if not math.isclose(trace[-1]["end_s"] - trace[0]["start_s"], trial["cpu_window_s"], rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("CPU interval durations do not match trial CPU window")
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


def run_schedule(modes: list[Mode], seed: int,
                 synthesize: Callable[[Mode], tuple[float, float, CpuMeasurement]],
                 log: list[dict[str, Any]]) -> list[Trial]:
    """Only one callback runs at a time; all model initialization is external."""
    schedule = make_schedule(modes, seed)
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
    colors = ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873"]
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
    colors = {mode["key"]: color for mode, color in zip(modes, ["#245fbd", "#078879", "#9c5caa", "#c77719", "#a54453", "#566873"] * len(modes))}
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


def cpu_profile_html(result: dict[str, Any]) -> str:
    if result["schema_version"] < 3:
        return ""
    groups = []
    for mode in result["modes"]:
        choices = []
        for index, trial in enumerate(result["trials"]):
            if trial["mode"] == mode["key"]:
                choices.append(f'<option value="{index}">#{trial["order"]} · block {trial["block"]} / {trial["trial"]} · {trial["elapsed_s"]:.3f}s</option>')
        groups.append(f'<optgroup label="{html.escape(mode["label"], quote=True)}">{"".join(choices)}</optgroup>')
    first_mode = result["modes"][0]["key"]
    median = statistics.median(row["elapsed_s"] for row in result["trials"] if row["mode"] == first_mode)
    default = min((index for index, row in enumerate(result["trials"]) if row["mode"] == first_mode),
                  key=lambda index: abs(result["trials"][index]["elapsed_s"] - median))
    labels = {mode["key"]: mode["label"] for mode in result["modes"]}
    data = [{"label": labels[row["mode"]], "order": row["order"], "incomplete": row["cpu_incomplete"], "trace": row["cpu_trace"]}
            for row in result["trials"]]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f'''<h2>CPU 使用率の時間推移</h2>
<label for="cpu-profile-trial">表示する試行 </label><select id="cpu-profile-trial" data-default="{default}">{''.join(groups)}</select>
<figure><svg id="cpu-profile" viewBox="0 0 880 250" role="img" aria-labelledby="cpu-profile-title"><title id="cpu-profile-title">区間平均CPU使用率</title></svg>
<figcaption>区間平均、100%＝論理1コア。横軸0は合成要求開始（初期カウンター採取は直前）。内部フェーズは未計測。短い区間はカウンター粒度の影響を受ける</figcaption></figure>
<script id="cpu-profile-data" type="application/json">{payload}</script>''' + r'''
<script>
(() => {
 const trials=JSON.parse(document.getElementById('cpu-profile-data').textContent), select=document.getElementById('cpu-profile-trial'), svg=document.getElementById('cpu-profile');
 const add=(tag,attrs={},text='')=>{const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text)e.textContent=text;svg.appendChild(e);return e;};
 function draw(){
  const trial=trials[Number(select.value)], points=trial.trace, xmin=Math.min(0,points[0].start_s), xmax=points[points.length-1].end_s;
  const ymax=Math.max(100,Math.ceil(Math.max(...points.map(p=>p.cpu_percent))/100)*100), left=70,top=35,w=780,h=165;
  const x=t=>left+(t-xmin)/(xmax-xmin)*w,y=v=>top+h-v/ymax*h;
  svg.replaceChildren();add('title',{id:'cpu-profile-title'},`${trial.label} 試行 #${trial.order} の区間平均CPU使用率`);
  add('text',{x:left,y:18},`${trial.label} · #${trial.order}${trial.incomplete?' · CPU追跡不完全':''}`);
  for(let i=0;i<=4;i++){const v=ymax*i/4;add('line',{x1:left,y1:y(v),x2:left+w,y2:y(v),stroke:'#e2e6ec'});add('text',{x:left-10,y:y(v)+4,'text-anchor':'end'},`${v.toFixed(0)}%`);}
  for(let i=0;i<=4;i++){const v=xmin+(xmax-xmin)*i/4;add('text',{x:x(v),y:221,'text-anchor':'middle'},v.toFixed(2));}
  const path=points.map((p,i)=>`${i?'L':'M'}${x(p.start_s).toFixed(2)},${y(p.cpu_percent).toFixed(2)}L${x(p.end_s).toFixed(2)},${y(p.cpu_percent).toFixed(2)}`).join('');
  add('path',{d:path,fill:'none',stroke:'#2563ad','stroke-width':1.6});
  add('text',{x:left+w/2,y:245,'text-anchor':'middle'},'合成要求開始からの時間（秒）');
 }
 select.value=select.dataset.default;select.addEventListener('change',draw);draw();
})();
</script>'''


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
    document = f'''<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOICEVOX CORE benchmark</title>
<style>
:root{{font-family:system-ui,-apple-system,sans-serif;color:#202936;background:#fff;font-size:15px;line-height:1.5}}body{{max-width:960px;margin:36px auto;padding:0 24px}}h1{{font-size:24px;letter-spacing:-.03em;margin:0 0 6px}}h2{{font-size:17px;margin:28px 0 10px}}p{{margin:6px 0 18px;color:#546170}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:9px 12px;border-bottom:1px solid #e2e6ec;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{font-weight:600;background:#f6f8fa}}figure{{margin:18px 0}}figcaption{{color:#546170;font-size:13px}}svg{{width:100%;height:auto}}svg text{{font-size:12px;fill:#546170}}details{{margin:18px 0;border-top:1px solid #d8dee7;padding-top:12px}}summary{{cursor:pointer;font-weight:600}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f6f8fa;padding:14px;max-height:420px;overflow:auto}}button{{font:inherit;background:#fff;border:1px solid #a8b4c4;border-radius:4px;padding:6px 12px;cursor:pointer;margin-top:12px}}.scroll{{overflow-x:auto}}.environment th{{text-align:left;width:34%}}.environment td{{text-align:left;overflow-wrap:anywhere}}ul{{padding-left:22px}}@media(max-width:600px){{body{{margin:20px auto;padding:0 14px}}th,td{{padding:7px}}}}
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
<li>各ラウンドでモード順をシャッフルし、1 ブロック内は 5 回連続。3 ラウンド、seed = {result["seed"]}。モード間の同時実行なし</li>
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
use voicevox_core::{AccelerationMode, AudioQuery, StyleId, blocking::{Onnxruntime, Synthesizer, VoiceModelFile}};
static SYNTH: OnceLock<Synthesizer<()>> = OnceLock::new();
static QUERY: OnceLock<AudioQuery> = OnceLock::new();
static WAV: Mutex<Vec<u8>> = Mutex::new(Vec::new());
static RAW: Mutex<Vec<u8>> = Mutex::new(Vec::new());
fn setup(runtime: &'static Onnxruntime, model: &str, query: &str, threads: u16) -> anyhow::Result<()> {
    let synth = Synthesizer::builder(runtime).acceleration_mode(AccelerationMode::Cpu).cpu_num_threads(threads).build()?;
    synth.load_voice_model(&VoiceModelFile::open(model)?).perform()?;
    let query: AudioQuery = serde_json::from_slice(&std::fs::read(query)?)?;
    query.validate()?;
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
pub extern "C" fn bench_init(threads: u16) -> i32 {
    let result = browser_runtime(threads).and_then(|runtime| setup(runtime, "/sample.vvm", "/query.json", threads));
    match result { Ok(()) => 0, Err(error) => { eprintln!("{error:#}"); 1 } }
}
#[cfg(target_os="emscripten")]
fn browser_runtime(threads: u16) -> anyhow::Result<&'static Onnxruntime> {
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
            .with_intra_threads(threads.into())?.with_inter_threads(1)?;
        ensure!(ort::init().with_name("voicevox_benchmark").with_global_thread_pool(pool).commit(), "ORT environment already initialized");
    }
    #[cfg(not(feature="threaded"))]
    let _ = threads;
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
fn main() {}
#[cfg(not(target_os="emscripten"))]
fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure!(args.len() == 7, "Expected runtime, model, query, threads, style, wav destination");
    let runtime = Onnxruntime::load_once().filename(&args[1]).perform()?;
    let threads = args[4].parse()?;
    let style = args[5].parse()?;
    setup(runtime, &args[2], &args[3], threads)?;
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
let configuredThreads=1, threaded=false, ready=false;
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
    onAbort:failure,
    onRuntimeInitialized:async()=>{
     try{
      for(const file of ['sample.vvm','query.json']){
       const response=await fetch('/'+file);if(!response.ok)throw new Error('HTTP '+response.status+' '+file);
       Module.FS.writeFile('/'+file,new Uint8Array(await response.arrayBuffer()));
      }
      if(Module._bench_init(configuredThreads)!==0)throw new Error('CORE initialization failed');
      Module.FS.unlink('/sample.vvm');Module.FS.unlink('/query.json');ready=true;
      const pthreads=threaded ? Module.PThread.runningWorkers.length : 0;
      if(threaded && configuredThreads>1 && pthreads<configuredThreads-1)
       throw new Error('ORT did not create the requested inference pthreads');
      postMessage({ready:true,threads:configuredThreads,shared_memory:Module.HEAPU8.buffer instanceof SharedArrayBuffer,pthreads_created:pthreads});
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


CORE_BENCH_HELPER = '''        // Untimed diagnostic only: mirrors the pinned TalkDomain synthesis path
        // up to decode(), before PCM conversion/resampling. Not called by synthesis().
        #[doc(hidden)]
        pub fn benchmark_raw_synthesis(&self, audio_query: &AudioQuery, style_id: StyleId) -> crate::Result<Vec<f32>> {
            let audio_query = audio_query.to_validated()?;
            let super::DecoderFeature { f0, phoneme } = audio_query.decoder_feature(super::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK);
            self.0.decode(f0.len(), super::PhonemeCode::num_phoneme(), &f0, phoneme.as_flattened(), style_id, ()).block_on()
        }

'''


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
    def __init__(self, browser: Any, url: str, module: str, threads: int, threaded: bool, style: int, wav: Path):
        self.page = browser.new_page()
        self.page.set_default_timeout(600_000)
        self.page.goto(url, wait_until="load")
        message = self.page.evaluate("data => request(data)", {"command": "init", "module": module, "threads": threads, "threaded": threaded})
        if not message.get("ready") or message.get("threads") != threads or message.get("shared_memory") != threaded:
            self.page.close()
            raise RuntimeError("Browser did not confirm the required thread/shared-memory configuration")
        self.style, self.wav, self.duration, self.bytes = style, wav, 0.0, 0
        self.shared_memory = message["shared_memory"]
        self.pthreads_created = message.get("pthreads_created", 0)

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
                 optimization: str = "z", graph_level: int = 1) -> Path:
    if optimization not in ("z", "3") or graph_level not in (1, 3):
        raise ValueError("Unsupported benchmark optimization variant")
    write_wrapper(source)
    kind = "native" if threaded is None else ("browser-mt" if threaded else "browser-st")
    if optimization != "z":
        kind += "-o3"
    if graph_level != 1:
        kind += "-graph3"
    pool = 'Module["benchmarkPoolSize"]' if threaded else 0
    import inspect
    build_adapter = inspect.getsource(build_runner) + inspect.getsource(write_wrapper)
    identity = {"build_adapter_sha256": hashlib.sha256(build_adapter.encode()).hexdigest(), "core": CORE_COMMIT, "rust": RUST_VERSION, "emscripten": EMSDK_VERSION,
                "runtime": sha256_file(runtime), "wrapper": hashlib.sha256(RUST_SOURCE.encode()).hexdigest(),
                "core_diagnostic_patch": hashlib.sha256(CORE_BENCH_HELPER.encode()).hexdigest(),
                "kind": kind, "optimization": optimization, "graph_level": graph_level,
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
        flags = ["-C", f"target-feature={features}", "-C", "link-arg=-msimd128", "-C", "link-arg=-fwasm-exceptions", "-C", "link-arg=-sALLOW_MEMORY_GROWTH=1",
                 "-C", "link-arg=-sINITIAL_MEMORY=1073741824", "-C", "link-arg=-sSTACK_SIZE=8388608",
                 "-C", "link-arg=-sEXPORTED_RUNTIME_METHODS=FS,HEAPU8" + (",PThread" if threaded else ""),
                 "-C", "link-arg=-sEXPORTED_FUNCTIONS=_main,_bench_init,_bench_synthesize,_bench_wav_ptr,_bench_wav_len,_bench_raw,_bench_raw_ptr,_bench_raw_len",
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


def verify_threaded_runtime(folder: Path) -> dict[str, Any]:
    info_path = find_one(folder, "BUILD_INFO.json")
    info = json.loads(info_path.read_text("utf-8"))
    expected = {"schema_version": 1, "library": "onnxruntime", "version": ORT_VERSION,
                "source_commit": "a83fc4d58cb48eb68890dd689f94f28288cf2278",
                "emscripten_version": EMSDK_VERSION, "target": "wasm32-unknown-emscripten",
                "simd": True, "pthreads": True, "signed": False, "exception_abi": "wasm", "thread_pool_scope": "global"}
    if any(info.get(key) != value for key, value in expected.items()):
        raise RuntimeError("The threaded archive does not match the required generic ORT/SIMD/pthread build")
    if not info.get("smoke_test", {}).get("passed"):
        raise RuntimeError("The threaded runtime's build smoke test did not pass")
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
    if args.experiments and args.baseline_only:
        raise ValueError("--experiments requires the multithreaded browser reference")
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
    if not args.baseline_only and not threaded_url and not args.threaded_ort_archive:
        raise RuntimeError("The unsigned multithreaded ORT release is not yet configured. Supply its verified archive URL using --threaded-ort-url; single-thread results are not a substitute for this mode.")
    begin = time.monotonic()
    progress(f"Preparing Rust {RUST_VERSION} and Emscripten {EMSDK_VERSION}")
    env = ensure_toolchains(root)
    progress("Preparing CORE source and ONNX Runtime archives")
    core_archive = cached_download(f"https://github.com/yamachu/voicevox_core/archive/{CORE_COMMIT}.tar.gz", root, f"core-{CORE_COMMIT}.tar.gz")
    core_tree = unpack_cached(core_archive, root / f"source-{CORE_COMMIT}")
    source = next(path.parent for path in core_tree.glob("*/Cargo.toml"))
    platform_name, _ = native_platform()
    runtime_archives = {}
    urls = {
        "native": f"{BASE_RELEASE}/onnxruntime-{platform_name}-{ORT_VERSION}.tgz",
        "browser": f"{BASE_RELEASE}/onnxruntime-wasm-static-{ORT_VERSION}.tgz",
    }
    if not args.baseline_only:
        urls["browser_mt"] = threaded_url
    for name, url in urls.items():
        if name == "browser_mt" and args.threaded_ort_archive:
            archive = args.threaded_ort_archive.resolve()
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
    native_runtime = find_one(runtime_archives["native"][1], library_name)
    st_runtime = find_one(runtime_archives["browser"][1], "libonnxruntime_webassembly.a")
    mt_runtime = find_one(runtime_archives["browser_mt"][1], "libonnxruntime_webassembly.a") if not args.baseline_only else None
    native_binary = build_runner(source, native_runtime, root, env, threaded=None, threads=args.threads)
    browser_st = build_runner(source, st_runtime, root, env, threaded=False, threads=1)
    browser_mt = build_runner(source, mt_runtime, root, env, threaded=True, threads=args.threads) if mt_runtime else None
    browser_binaries = {"st": browser_st}
    if browser_mt:
        browser_binaries["mt"] = browser_mt
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
    modes = [Mode("native", f"Native ×{args.threads}", args.threads, "native CPU, per-session thread pools"),
             Mode("browser", "Browser ×1", 1, "WebAssembly SIMD, unshared memory")]
    if not args.baseline_only:
        modes.append(Mode("browser_mt", f"Browser pthreads ×{args.threads}", args.threads, "WebAssembly SIMD + pthreads, global thread pool"))
    if args.experiments:
        modes.extend([
            Mode("browser_mt_threads", f"実験 MT ×{args.experimental_threads}", args.experimental_threads, "experimental: thread count only; global pool", experimental=True),
            Mode("browser_mt_o3", f"実験 MT ×{args.threads} O3", args.threads, "experimental: CORE opt-level=3 + final Emscripten -O3; existing LTO retained", core_optimization="3", experimental=True),
            Mode("browser_mt_graph3", f"実験 MT ×{args.threads} Graph L3", args.threads, "experimental: ORT GraphOptimizationLevel::Level3 (ORT_ENABLE_LAYOUT)", graph_optimization=3, experimental=True),
        ])
    log: list[dict[str, Any]] = [{"event": "assets_ready", "seconds": round(time.monotonic() - begin, 3)}]
    environment = environment_info()
    environment.update({"cpu_metric": "sum target-tree user+system CPU seconds / baseline-to-final snapshot seconds; 100%=one logical CPU", "cpu_sample_interval_ms": CPU_SAMPLE_INTERVAL_S * 1000,
                        "cpu_trace_clock": "actual snapshot-completion times relative to synthesis request start; baseline may be slightly negative; no internal phase attribution",
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
                                               "threads": mode.threads, "experimental": mode.experimental} for mode in modes}
    environment["wasm_bytes"] = {key: binary.with_suffix(".wasm").stat().st_size for key, binary in browser_binaries.items()}
    environment["precision_flags"] = "FP32 model; unchanged strict FP/SIMD flags; no fast-math, relaxed SIMD, FP16 or quantization"
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
                runners: dict[str, Any] = {}
                browsers: dict[str, Any] = {}
                cpu_roots: dict[str, int] = {}
                try:
                    progress("Initializing native CORE")
                    started = time.monotonic()
                    runners["native"] = NativeRunner(native_binary, native_runtime, model, work / "query.json", args.threads, args.style_id, work / "native.wav")
                    cpu_roots["native"] = runners["native"].process.pid
                    log.append({"event": "initialized", "mode": "native", "seconds": round(time.monotonic() - started, 3)})
                    browser_modes = [("browser", "/st/voicevox_benchmark.js", 1, False)]
                    if browser_mt:
                        browser_modes.append(("browser_mt", "/mt/voicevox_benchmark.js", args.threads, True))
                    if args.experiments:
                        browser_modes.extend([
                            ("browser_mt_threads", "/mt/voicevox_benchmark.js", args.experimental_threads, True),
                            ("browser_mt_o3", "/o3/voicevox_benchmark.js", args.threads, True),
                            ("browser_mt_graph3", "/graph3/voicevox_benchmark.js", args.threads, True),
                        ])
                    for key, module, threads, threaded in browser_modes:
                        progress(f"Initializing isolated {key} Chromium and CORE")
                        started = time.monotonic()
                        browser = playwright.chromium.launch(**options)
                        browsers[key] = browser
                        cpu_roots[key] = chromium_process_id(browser)
                        if "browser" in environment and environment["browser"] != browser.version:
                            raise RuntimeError("Browser versions differ between modes")
                        environment["browser"] = browser.version
                        runners[key] = BrowserRunner(browser, url, module, threads, threaded, args.style_id, work / f"{key}.wav")
                        environment[f"{key}_pthreads_created"] = runners[key].pthreads_created
                        log.append({"event": "initialized", "mode": key, "seconds": round(time.monotonic() - started, 3)})
                    if "browser_mt" in runners:
                        environment["browser_mt_pthreads_created"] = runners["browser_mt"].pthreads_created
                    environment.update(runners["browser"].page.evaluate("() => ({browser_hardware_concurrency:navigator.hardwareConcurrency,cross_origin_isolated:crossOriginIsolated,user_agent:navigator.userAgent})"))
                    for mode in modes:
                        progress(f"Warmup: {mode.label} (excluded from latency and CPU measurements)")
                        elapsed, duration = runners[mode.key].synthesize(save=True)
                        log.append({"event": "warmup_excluded", "mode": mode.key, "seconds": elapsed, "audio_s": duration})
                    durations = [runner.duration for runner in runners.values()]
                    if max(durations) - min(durations) > 1 / query["outputSamplingRate"]:
                        raise RuntimeError("Native/browser output lengths differ; refusing a misleading comparison")
                    output_checks = verify_outputs(runners, "browser_mt" if browser_mt else "browser", log)
                    progress(f"Starting {len(modes) * BLOCKS_PER_MODE * TRIALS_PER_BLOCK} serial trials with target-process CPU sampling")
                    def measured_synthesis(mode: Mode) -> tuple[float, float, CpuMeasurement]:
                        sampler = ProcessCpuSampler(cpu_roots[mode.key], mode.label)
                        return sampler.measure(runners[mode.key].synthesize)
                    trials = run_schedule(modes, args.seed, measured_synthesis, log)
                    result = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials],
                              "environment": environment, "audio_s": durations[0], "style_id": args.style_id, "seed": args.seed, "log": log,
                              "output_checks": output_checks,
                              "notes": ["出力検証は測定外。同じ実行のブラウザMT基準とPCM・PCM化前FP32を照合。非一致を精度維持とは判定しない。" if browser_mt else "出力検証は測定外。ブラウザST基準とPCM・PCM化前FP32を照合。"]}
                    if args.baseline_only:
                        result["notes"].append("この先行検証はnativeとブラウザ単一スレッドのみ。マルチスレッドは未実施。")
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
    parser.add_argument("--experimental-threads", type=positive_int, default=4, help="thread-count candidate used with --experiments; default: 4")
    parser.add_argument("--threaded-ort-archive", type=Path, help="local verified MT archive with adjacent .sha256 sidecar")
    parser.add_argument("--threaded-ort-url", help="verified unsigned threaded ORT archive from the fork release")
    parser.add_argument("--browser-path", type=Path, help="optional installed Chromium executable")
    parser.add_argument("--headed", action="store_true", help="show the otherwise identical browser runner")
    parser.add_argument("--self-test", action="store_true", help="check scheduling, validation and reporting without synthesizing")
    return parser


def self_test() -> None:
    progress("Self-test fixtures only; no real synthesis or CPU measurements")
    modes = [Mode("native", "Native", 2, "native CPU"), Mode("browser", "Browser", 1, "WebAssembly SIMD")]
    a = make_schedule(modes, 42)
    assert a == make_schedule(modes, 42)
    assert len(a) == 6 and all(sum(block.mode == mode.key for block in a) == 3 for mode in modes)
    assert max(1, logical_cpu_count() // 2) == default_threads()
    # Explicit test fixture, confined to a temporary directory and never delivered
    # as benchmark measurements.
    log: list[dict[str, Any]] = []
    def fixture(mode: Mode) -> tuple[float, float, CpuMeasurement]:
        trace = (CpuInterval(-0.001, 0.119, 0.06 * mode.threads, 50.0 * mode.threads, 1, False),
                 CpuInterval(0.119, 0.199, 0.04 * mode.threads, 50.0 * mode.threads, 1, False))
        return 0.1 * mode.threads, 10.0, CpuMeasurement(0.1 * mode.threads, 0.2, 0.5 * mode.threads,
                                                     50.0 * mode.threads, 3, 1, False, trace)
    trials = run_schedule(modes, 42, fixture, log)
    result = {"schema_version": SCHEMA_VERSION, "created_at": "SELF-TEST FIXTURE", "modes": [asdict(mode) for mode in modes], "trials": [asdict(trial) for trial in trials], "environment": {"test": "<>&"}, "audio_s": 10.0, "style_id": 302, "seed": 42, "log": log}
    validate_results(result)
    assert len(list(csv.DictReader(io.StringIO(trial_csv(result["trials"]))))) == 30
    assert len(list(csv.DictReader(io.StringIO(cpu_trace_csv(result["trials"]))))) == 60
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "test.html"
        render_report(result, report)
        content = report.read_text()
        assert '<details><summary>生データ（CSV）</summary>' in content
        assert '<details open' not in content
        assert '<summary>CPU 時系列（CSV）</summary>' in content
        assert 'id="cpu-profile-trial"' in content
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

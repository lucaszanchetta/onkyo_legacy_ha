#!/usr/bin/env python3
"""Measure client queue latency with deterministic simulated ISCP responses.

Runs the real OnkyoLegacyClient query/send methods and operation gate with
injected socket delays. The baseline swaps in a FIFO gate to model the old
non-prioritized serialization. No receiver or network socket is used.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.helpers import fresh_import


class FifoGate:
    """Deterministic FIFO serialization used as the legacy comparison."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiters: deque[object] = deque()
        self._active = False

    def acquire(self, _priority: int = 0) -> None:
        token = object()
        with self._condition:
            self._waiters.append(token)
            while self._active or self._waiters[0] is not token:
                self._condition.wait()
            self._waiters.popleft()
            self._active = True

    def release(self) -> None:
        with self._condition:
            self._active = False
            self._condition.notify_all()

    def __enter__(self) -> FifoGate:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _wait_for_waiters(gate: Any, count: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with gate._condition:
            if len(gate._waiters) >= count:
                return
        time.sleep(0.001)
    raise TimeoutError(f"expected {count} queued operations")


def run_sample(module: Any, *, prioritized: bool, optional_delay: float) -> tuple[float, float]:
    client = module.OnkyoLegacyClient("simulated-receiver", 60128, retries=1)
    if not prioritized:
        client._operation_lock = FifoGate()
    gate = client._operation_lock
    first_started = threading.Event()
    release_first = threading.Event()
    optional_calls = 0

    def simulated_query(command: str, timeout: float = 5.0) -> str:
        nonlocal optional_calls
        if command.startswith("OPT"):
            optional_calls += 1
            if optional_calls == 1:
                first_started.set()
                if not release_first.wait(2):
                    raise TimeoutError("benchmark release barrier timed out")
            time.sleep(optional_delay)
            raise module.CommandResponseTimeout("simulated silent optional response")
        time.sleep(0.020)
        return "CORE00"

    def simulated_send(command: str, value: str) -> None:
        time.sleep(0.005)

    client._query_once = simulated_query
    client._send_once = simulated_send
    background_errors: list[Exception] = []
    control_requested = [0.0]
    send_completed = [0.0]
    core_completed = [0.0]
    control_errors: list[Exception] = []

    def optional_call(command: str) -> None:
        try:
            client.query_optional(command, timeout=optional_delay)
        except Exception as err:
            background_errors.append(err)

    first = threading.Thread(target=optional_call, args=("OPT1",))
    first.start()
    if not first_started.wait(2):
        raise TimeoutError("first optional request did not start")

    second_requested = threading.Event()

    def second_optional() -> None:
        second_requested.set()
        optional_call("OPT2")

    second = threading.Thread(target=second_optional)
    second.start()
    if not second_requested.wait(1):
        raise TimeoutError("second optional request was not submitted")
    _wait_for_waiters(gate, 1)

    def control() -> None:
        control_requested[0] = time.perf_counter()
        try:
            client.send("MVL", "UP")
            send_completed[0] = time.perf_counter()
            client.query_batch(("CORE",))
            core_completed[0] = time.perf_counter()
        except Exception as err:
            control_errors.append(err)

    control_thread = threading.Thread(target=control)
    control_thread.start()
    _wait_for_waiters(gate, 2)
    release_first.set()
    for thread in (first, second, control_thread):
        thread.join(timeout=max(5, optional_delay * 4))
        if thread.is_alive():
            raise TimeoutError("benchmark worker did not finish")
    if background_errors and len(background_errors) != 2:
        raise RuntimeError(f"unexpected optional-query errors: {background_errors!r}")
    if control_errors:
        raise RuntimeError(f"control transaction failed: {control_errors!r}")
    send_queue_ms = (send_completed[0] - control_requested[0]) * 1000
    end_to_end_ms = (core_completed[0] - control_requested[0]) * 1000
    return send_queue_ms, end_to_end_ms


def report(samples: list[tuple[float, float]]) -> str:
    values = []
    for index, name in enumerate(("send_queue_ms", "control_core_ms")):
        column = [sample[index] for sample in samples]
        values.append(
            f"{name}=p50:{statistics.median(column):.1f},"
            f"p95:{sorted(column)[min(len(column) - 1, int(len(column) * .95))]:.1f},"
            f"max:{max(column):.1f}"
        )
    return " ".join(values)


def main(runs: int, timeout: float, normal_delay: float) -> None:
    module = fresh_import("custom_components.onkyo_legacy.coordinator")
    for scenario, delay in (("normal", normal_delay), ("silent-timeout", timeout)):
        for label, prioritized in (("fifo-baseline", False), ("priority-gate", True)):
            samples = [
                run_sample(module, prioritized=prioritized, optional_delay=delay)
                for _ in range(runs)
            ]
            print(f"{scenario:14} {label:14} {report(samples)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--normal-delay", type=float, default=0.02)
    args = parser.parse_args()
    main(args.runs, args.timeout, args.normal_delay)

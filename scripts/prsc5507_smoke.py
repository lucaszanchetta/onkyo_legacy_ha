#!/usr/bin/env python3
"""Live smoke tester for the Onkyo legacy integration."""

from __future__ import annotations

import argparse
import binascii
import json
import select
import socket
from pathlib import Path
from typing import Any

from eiscp import eISCP
from eiscp import commands as eiscp_commands
from eiscp.core import ISCPMessage
from eiscp.core import eISCPPacket
from eiscp.core import parse_info

eISCP.CONNECT_TIMEOUT = 2

DEFAULT_HOST = "192.168.1.23"
DEFAULT_PORT = 60128
DEFAULT_MODEL = "PR-SC5507"
TX8050_MODEL = "TX-8050"
LEGACY_PROBE_COMMANDS = ("PWR", "MVL", "AMT", "SLI", "LMD")

QUERY_COMMANDS_BY_MODEL = {
    DEFAULT_MODEL: (
        "PWR",
        "MVL",
        "AMT",
        "SLI",
        "ZPW",
        "ZVL",
        "ZMT",
        "SLZ",
        "PW3",
        "VL3",
        "MT3",
        "SL3",
        "SLA",
        "LMD",
        "DIM",
        "SLP",
        "CTL",
        "SWL",
        "SW2",
        "TGA",
        "TGB",
        "TGC",
        "PMB",
        "LTN",
        "RAS",
        "ADQ",
        "ADV",
        "MOT",
        "AVS",
        "PBS",
        "SBS",
        "HAO",
        "HAS",
        "CEC",
        "ARC",
        "RES",
        "HDO",
        "IFA",
        "IFV",
        "FLD",
    ),
    TX8050_MODEL: (
        "PWR",
        "MVL",
        "AMT",
        "SLI",
        "ZPW",
        "ZVL",
        "ZMT",
        "SLZ",
        "LMD",
        "DIM",
        "SLP",
        "TUN",
    ),
}

SAFE_WRITE_TESTS_BY_MODEL = {
    DEFAULT_MODEL: {
        "DIM": ("00", "01"),
        "SLP": ("00", "1E"),
        "SLA": ("00", "04"),
        "LTN": ("00", "01"),
        "RAS": ("00", "01"),
        "ADQ": ("00", "01"),
        "ADV": ("00", "01"),
        "MOT": ("00", "01"),
        "AMT": ("00", "01"),
        "TGA": ("00", "01"),
        "TGB": ("00", "01"),
        "TGC": ("00", "01"),
        "ZMT": ("00", "01"),
        "MT3": ("00", "01"),
        "ZVL": ("00", "28"),
        "VL3": ("00", "28"),
    },
    TX8050_MODEL: {
        "AMT": ("00", "01"),
        "ZMT": ("00", "01"),
        "MVL": ("00", "28"),
        "ZVL": ("00", "28"),
        "DIM": ("00", "01"),
        "SLP": ("00", "1E"),
    },
}


def classify(exc: Exception | None, response: str | None) -> str:
    if exc is None:
        return "queryable"
    name = type(exc).__name__
    if name == "ValueError" and "Timeout waiting for response" in str(exc):
        return "timeout"
    if name == "AssertionError":
        return "assertion_failure"
    return name


def parse_command_list(raw: str) -> tuple[str, ...]:
    commands = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not commands:
        raise ValueError("At least one command must be supplied.")
    return commands


def run_query(command: str, host: str, port: int) -> dict[str, Any]:
    receiver = eISCP(host, port)
    try:
        try:
            response = receiver.raw(f"{command}QSTN")
            return {"command": command, "result": "queryable", "response": response}
        except Exception as exc:
            return {
                "command": command,
                "result": classify(exc, None),
                "response": None,
                "error": str(exc),
            }
    finally:
        receiver.disconnect()


def run_discovery_probe(host: str, port: int, timeout: float) -> dict[str, Any]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    try:
        sock.sendto(eISCPPacket("!xECNQSTN").get_raw(), (host, port))
        ready = select.select([sock], [], [], timeout)
        if not ready[0]:
            return {"result": "timeout", "response": None}

        data = sock.recv(1024)
        response = eISCPPacket.parse(data)
        parsed = None
        error = None
        try:
            parsed = parse_info(data)
        except Exception as exc:  # broad to preserve raw response
            error = f"{type(exc).__name__}: {exc}"
        return {"result": "queryable", "response": response, "parsed": parsed, "error": error}
    except Exception as exc:
        return {
            "result": classify(exc, None),
            "response": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        sock.close()


def _format_raw_response(data: bytes | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return {
        "hex": binascii.hexlify(data).decode("ascii"),
        "ascii": "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data),
        "length": len(data),
    }


def _raw_probe_payloads(command: str) -> tuple[tuple[str, bytes], ...]:
    base = command.upper()
    wrapped = eISCPPacket(ISCPMessage(base)).get_raw()
    return (
        ("wrapped_cr", wrapped),
        ("wrapped_eof_cr", eISCPPacket(f"!1{base}\x1a\r").get_raw()),
        ("wrapped_eof_crlf", eISCPPacket(f"!1{base}\x1a\r\n").get_raw()),
        ("plain_cr", f"!1{base}\r".encode("ascii")),
        ("plain_eof_cr", f"!1{base}\x1a\r".encode("ascii")),
        ("plain_eof_crlf", f"!1{base}\x1a\r\n".encode("ascii")),
    )


def run_raw_probe(command: str, host: str, port: int, timeout: float) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for label, payload in _raw_probe_payloads(command):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        response: bytes | None = None
        error = None
        try:
            sock.connect((host, port))
            sock.sendall(payload)
            try:
                response = sock.recv(4096)
            except socket.timeout:
                pass
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            sock.close()

        result = "queryable" if response else "timeout"
        if error:
            result = classify(RuntimeError(error), None)
        results.append(
            {
                "variant": label,
                "sent": _format_raw_response(payload),
                "response": _format_raw_response(response),
                "result": result,
                "error": error,
            }
        )
    return results


def run_write_test(command: str, values: tuple[str, str], host: str, port: int) -> dict[str, Any]:
    receiver = eISCP(host, port)
    before = after = None
    error = None
    try:
        try:
            before = receiver.raw(f"{command}QSTN")
            before_code = payload_code(before, command)
            target = values[0] if before_code == values[1] else values[1]
            receiver.send(f"{command}{target}")
            after = receiver.raw(f"{command}QSTN")
            receiver.send(f"{command}{before_code}")
            receiver.raw(f"{command}QSTN")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    finally:
        receiver.disconnect()
    return {"command": command, "before": before, "after": after, "error": error}


def payload_code(response: str, command: str) -> str:
    if not response.startswith(command):
        raise ValueError(f"Unexpected response for {command}: {response}")
    return response[len(command) :].upper()


def command_zone(command: str) -> str:
    for zone in ("main", "zone2", "zone3", "zone4", "dock"):
        if command in eiscp_commands.COMMANDS.get(zone, {}):
            return zone
    raise KeyError(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--commands",
        help="Comma-separated raw command prefixes to query, e.g. PWR,MVL,AMT,SLI,LMD.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=eISCP.CONNECT_TIMEOUT,
        help="TCP and UDP probe timeout in seconds.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Also send a direct UDP ECNQSTN discovery probe to the target host.",
    )
    parser.add_argument(
        "--raw-probe",
        action="store_true",
        help="Also try low-level TCP framing variants for the first queried command.",
    )
    parser.add_argument(
        "--writes",
        action="store_true",
        help="Run reversible write tests for a small safe subset.",
    )
    args = parser.parse_args()

    eISCP.CONNECT_TIMEOUT = args.timeout
    model = args.model.strip().upper()
    query_commands = QUERY_COMMANDS_BY_MODEL.get(model)
    if args.commands:
        query_commands = parse_command_list(args.commands)
    elif query_commands is None:
        query_commands = LEGACY_PROBE_COMMANDS

    results = [run_query(command, args.host, args.port) for command in query_commands]
    payload: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "model": model,
        "timeout": args.timeout,
        "queries": results,
    }

    if args.discover:
        payload["discovery"] = run_discovery_probe(args.host, args.port, args.timeout)

    if args.raw_probe:
        payload["raw_probe"] = run_raw_probe(f"{query_commands[0]}QSTN", args.host, args.port, args.timeout)

    if args.writes:
        if model not in SAFE_WRITE_TESTS_BY_MODEL:
            raise SystemExit(f"Writes are not supported for model: {args.model}")
        write_tests = SAFE_WRITE_TESTS_BY_MODEL[model]
        payload["writes"] = [
            run_write_test(command, values, args.host, args.port)
            for command, values in write_tests.items()
        ]
        payload["write_value_options"] = {
            command: list(
                eiscp_commands.COMMANDS[command_zone(command)][command]["values"].keys()
            )
            for command in write_tests
        }

    text = json.dumps(payload, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

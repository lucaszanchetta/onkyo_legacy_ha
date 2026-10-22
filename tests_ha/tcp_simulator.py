"""Small local TCP receiver used by real-client integration tests."""

from __future__ import annotations

import asyncio
import struct


def encode_frame(message: str) -> bytes:
    body = f"!1{message}\x1a\r".encode()
    return b"ISCP" + struct.pack("!IIB3s", 16, len(body), 1, b"\0\0\0") + body


class ReceiverSimulator:
    """Serve one serialized ISCP connection and allow deterministic fault modes."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.port = 0
        self.commands: list[str] = []
        self.mode = "normal"
        self.eof_once = False
        self.volume = 20
        self.requests = asyncio.Event()
        self.silent_request = asyncio.Event()
        self.held_request = asyncio.Event()
        self.hold_release = asyncio.Event()
        self.hold_release.set()
        self.active_connections = 0
        self.accepted_connections = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> ReceiverSimulator:
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        assert self.server.sockets
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        self.hold_release.set()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted_connections += 1
        self.active_connections += 1
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(16)
                magic, header_size, data_size, _version, _reserved = struct.unpack(
                    "!4sIIB3s", header
                )
                if magic != b"ISCP" or header_size != 16:
                    return
                body = await reader.readexactly(data_size)
                command = body.decode("utf-8")[2:].rstrip("\r\n\x1a")
                self.commands.append(command)
                self.requests.set()
                if self.mode == "silent":
                    self.silent_request.set()
                    continue
                if self.mode == "hold":
                    self.held_request.set()
                    await self.hold_release.wait()
                if self.mode == "delayed":
                    await asyncio.sleep(0.03)
                if self.mode == "push_before_response" and command == "PWRQSTN":
                    writer.write(encode_frame("MVL2A"))
                    await writer.drain()
                    await asyncio.sleep(0.02)
                if self.eof_once and command == "PWRQSTN":
                    self.eof_once = False
                    writer.write(
                        b"ISCP" + struct.pack("!IIB3s", 16, 12, 1, b"\0\0\0") + b"!1PWR"
                    )
                    await writer.drain()
                    writer.close()
                    return
                response = self.response_for(command)
                if response is not None:
                    packet = encode_frame(response)
                    if self.mode == "fragmented":
                        for start, end in ((0, 3), (3, 16), (16, 19), (19, len(packet))):
                            writer.write(packet[start:end])
                            await writer.drain()
                            await asyncio.sleep(0.002)
                    else:
                        writer.write(packet)
                        await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.active_connections -= 1

    def response_for(self, command: str) -> str | None:
        if not command.endswith("QSTN"):
            return None
        prefix = command[:3]
        values = {
            "PWR": "01",
            "MVL": f"{self.volume:02X}",
            "AMT": "00",
            "SLI": "00",
            "ZPW": "00",
            "ZVL": "00",
            "ZMT": "00",
            "SLZ": "00",
            "PW3": "00",
            "VL3": "00",
            "MT3": "00",
            "SL3": "00",
            "LMD": "00",
            "SLA": "00",
            "LTN": "00",
            "RAS": "00",
            "ADQ": "00",
            "ADV": "00",
            "MOT": "00",
            "TGA": "00",
            "TGB": "00",
            "TGC": "00",
            "DIM": "00",
            "CTL": "00",
            "SWL": "00",
            "SLP": "00",
            "IFA": "HDMI 1,PCM,48kHz,2.0ch,Stereo,All Ch Stereo,48kHz",
            "IFV": "HDMI 1,1920x1080p,RGB,8bit,HDMI,1920x1080p,RGB,8bit,Standard",
            "RES": "1920x1080p",
            "HDO": "HDMI OUT",
        }
        payload = values.get(prefix)
        return f"{prefix}{payload}" if payload is not None else None

    async def wait_for_command(self, command: str, timeout: float = 1.0) -> None:
        async def predicate() -> None:
            while command not in self.commands:
                self.requests.clear()
                await self.requests.wait()

        await asyncio.wait_for(predicate(), timeout)

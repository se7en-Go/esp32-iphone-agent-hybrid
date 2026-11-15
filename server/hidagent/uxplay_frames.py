"""Strict, bounded native UxPlay session/JPEG records; no raw-JPEG fallback."""

import struct
from dataclasses import dataclass

HEADER = struct.Struct("!4sBBHQQII")
MAX_JPEG = 8 * 1024 * 1024
BEGIN, JPEG, END = 1, 2, 3


class FrameProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class FrameRecord:
    kind: int
    epoch: int
    sequence: int
    payload: bytes = b""


class FrameStreamParser:
    """Accept one stream, ignoring only fully validated, retired epochs.

    Every record is parsed before publication. A malformed stream is terminal;
    neither magic scanning nor a later BEGIN can make it valid again.
    """

    def __init__(self):
        self.epoch = self.sequence = 0
        self.active = False
        self._buffer = bytearray()
        self._pending = None
        self._failed = self._finished = False

    def _error(self, message):
        self._failed = True
        self._buffer.clear()
        self._pending = None
        raise FrameProtocolError(message)

    def _header(self):
        magic, version, kind, flags, epoch, sequence, size, reserved = HEADER.unpack(self._buffer)
        self._buffer.clear()
        if magic != b"HAFE" or version != 1 or kind not in (BEGIN, JPEG, END) or flags or reserved:
            self._error("invalid native frame header")
        if not epoch:
            self._error("invalid native session epoch")
        if kind == JPEG:
            if not sequence or not 4 <= size <= MAX_JPEG:
                self._error("invalid native JPEG sequence or size")
        elif sequence or size:
            self._error("native control record has payload or sequence")
        return kind, epoch, sequence, size

    def _record(self, kind, epoch, sequence, payload):
        if kind == JPEG and (not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9")):
            self._error("incomplete native JPEG")
        if epoch < self.epoch:
            return None  # validated stale callbacks never publish or renew a timestamp
        if kind == BEGIN:
            if epoch <= self.epoch:
                self._error("native BEGIN epoch did not increase")
            if self.active:
                self._error("native BEGIN arrived before previous END")
            self.epoch, self.sequence, self.active = epoch, 0, True
        else:
            if epoch != self.epoch or not self.active:
                self._error("native record has no active BEGIN")
            if kind == END:
                self.active = False
            elif sequence <= self.sequence:
                self._error("native JPEG sequence did not increase")
            else:
                self.sequence = sequence
        return FrameRecord(kind, epoch, sequence, payload)

    def feed(self, data: bytes) -> list[FrameRecord]:
        if self._failed or self._finished:
            raise FrameProtocolError("native frame stream is closed")
        accepted = []
        incoming = memoryview(data)
        offset = 0
        while offset < len(incoming):
            needed = HEADER.size if self._pending is None else self._pending[3]
            count = min(needed - len(self._buffer), len(incoming) - offset)
            self._buffer.extend(incoming[offset:offset + count])
            offset += count
            if len(self._buffer) != needed:
                continue
            if self._pending is None:
                self._pending = self._header()
                if self._pending[3]:
                    continue
            kind, epoch, sequence, _size = self._pending
            payload = bytes(self._buffer)
            self._buffer.clear()
            self._pending = None
            record = self._record(kind, epoch, sequence, payload)
            if record is not None:
                accepted.append(record)
        return accepted

    def finish(self):
        if self._failed or self._finished:
            raise FrameProtocolError("native frame stream is closed")
        if self._buffer or self._pending is not None:
            self._error("truncated native frame record")
        if self.active:
            self._error("native stream ended before session END")
        self._finished = True

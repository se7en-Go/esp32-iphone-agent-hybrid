"""Opt-in, bounded local pointer evidence. Never included in API responses or logs."""

import json
import os
import time
from pathlib import Path


class PointerDiagnostics:
    MAX_FRAMES = 48

    def __init__(self, directory: Path, *, prefix="pointer"):
        if prefix not in {"pointer", "action-admission"}:
            raise ValueError("invalid diagnostic prefix")
        self.directory = directory
        self.prefix = prefix
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.count = 0
        self.pinned = 0
        self.frames = {}
        self.events = []
        self.disabled = False
        if directory.is_symlink():
            raise OSError('diagnostic directory must not be a symlink')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        for path in directory.glob(f'{self.prefix}-frame-*.jpg'):
            path.unlink()
        self._write(f'{self.prefix}-attempt.json', b'{}')

    @classmethod
    def from_env(cls, *, prefix="pointer"):
        directory = os.environ.get('HIDAGENT_POINTER_DIAGNOSTICS')
        if not directory:
            return None
        try:
            return cls(Path(directory), prefix=prefix)
        except OSError:
            return None  # Diagnostics must never change the physical operation.

    def _write(self, name: str, data: bytes):
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(self.directory / name, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)

    def _metadata(self):
        self._write(f'{self.prefix}-attempt.json', json.dumps({
            'frames': sorted(self.frames.values(), key=lambda item: item['sequence']),
            'events': self.events,
        }, ensure_ascii=False).encode())

    def capture(self, phase: str, frame: bytes, received_at: float | None = None):
        if self.disabled:
            return
        try:
            # Keep the original evidence even when an extended outward leg
            # fills the ring. Only motion frames may overwrite motion frames.
            if self.count < 3 and phase in {"baseline", "target_reference", "position_reference"}:
                slot = self.count
                self.pinned = self.count + 1
            else:
                slot = self.pinned + (self.count - self.pinned) % (self.MAX_FRAMES - self.pinned)
            filename = f'{self.prefix}-frame-{slot:02d}.jpg'
            self._write(filename, frame)
            self.frames[slot] = {
                'file': filename, 'phase': phase, 'sequence': self.count,
                'relative_s': round(time.monotonic() - self.started, 6),
                'received_relative_s': (round(received_at - self.started_wall, 6)
                                        if received_at is not None else None),
                'age_s': round(time.time() - received_at, 6) if received_at is not None else None,
            }
            self.count += 1
            self._metadata()
        except OSError:
            self.disabled = True

    def event(self, phase: str, **fields):
        if self.disabled:
            return
        try:
            self.events.append({'phase': phase, 'relative_s': round(time.monotonic() - self.started, 6), **fields})
            self.events = self.events[-96:]
            self._metadata()
        except OSError:
            self.disabled = True

"""Carry semantic Send provenance through positioning to the HID write gate."""

from contextlib import contextmanager
from contextvars import ContextVar

from .ocr import normalize_text

named_send: ContextVar[bool] = ContextVar("named_send", default=False)


def is_send_target(query: str, label: str) -> bool:
    # Include the resolved label so a substring query cannot bypass its semantics.
    return any(normalize_text(text) in {"发送", "發送", "send"} for text in (query, label))


@contextmanager
def send_target_scope(is_send: bool):
    # Preserve an enclosing named-layout send; ordinary taps cannot clear it.
    token = named_send.set(named_send.get() or is_send)
    try:
        yield
    finally:
        named_send.reset(token)

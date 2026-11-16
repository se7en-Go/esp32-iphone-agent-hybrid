"""Native session records are indivisible and cannot renew an old phone image."""

import struct

import pytest

from hidagent.uxplay_frames import FrameProtocolError, FrameStreamParser

HEADER = struct.Struct("!4sBBHQQII")
JPEG = b"\xff\xd8test-jpeg\xff\xd9"


def record(kind, epoch=1, sequence=0, payload=b"", *, magic=b"HAFE", version=1, flags=0, reserved=0):
    return HEADER.pack(magic, version, kind, flags, epoch, sequence, len(payload), reserved) + payload


def test_fragmented_headers_and_jpegs_publish_only_complete_records():
    parser = FrameStreamParser()
    wire = record(1) + record(2, sequence=1, payload=JPEG) + record(3)
    accepted = []
    for byte in wire:
        accepted.extend(parser.feed(bytes([byte])))
    parser.finish()
    assert [(x.kind, x.epoch, x.sequence, x.payload) for x in accepted] == [
        (1, 1, 0, b""), (2, 1, 1, JPEG), (3, 1, 0, b"")]


def test_new_begin_retires_previous_epoch_and_ignores_validated_late_callbacks():
    parser = FrameStreamParser()
    accepted = parser.feed(record(1) + record(2, sequence=1, payload=JPEG) + record(3) + record(1, epoch=2)
                           + record(2, epoch=1, sequence=2, payload=JPEG) + record(3, epoch=1)
                           + record(2, epoch=2, sequence=1, payload=JPEG) + record(3, epoch=2))
    parser.finish()
    assert [(x.kind, x.epoch) for x in accepted] == [(1, 1), (2, 1), (3, 1), (1, 2), (2, 2), (3, 2)]


@pytest.mark.parametrize("wire", [
    record(1, magic=b"RAW!"), record(1, version=3), record(0), record(4), record(1, flags=1),
    record(1, reserved=1), record(1, epoch=0), record(1, sequence=1), record(3, sequence=1),
    record(1, payload=b"x"), record(3, payload=b"x"), record(2, sequence=1, payload=JPEG),
    record(1) + record(1), record(1) + record(1, epoch=2), record(1) + record(3, epoch=2),
    record(1) + record(2, epoch=2, sequence=1, payload=JPEG),
    record(1) + record(2, payload=JPEG), record(1) + record(2, sequence=1, payload=b"bad!"),
    record(1) + record(2, sequence=1, payload=b"\xff\xd8\xff\x00"),
    record(1) + record(2, sequence=1, payload=JPEG) + record(2, sequence=1, payload=JPEG),
    record(1) + record(2, sequence=3, payload=JPEG) + record(2, sequence=2, payload=JPEG),
    record(1) + record(3) + record(2, sequence=1, payload=JPEG), record(1) + record(3) + record(3),
])
def test_invalid_stream_is_terminal_and_never_resynchronizes(wire):
    parser = FrameStreamParser()
    with pytest.raises(FrameProtocolError):
        parser.feed(wire)
    with pytest.raises(FrameProtocolError):
        parser.feed(record(1, epoch=100))


def test_oversized_length_fails_before_waiting_for_or_allocating_payload():
    parser = FrameStreamParser()
    parser.feed(record(1))
    with pytest.raises(FrameProtocolError, match="size"):
        parser.feed(HEADER.pack(b"HAFE", 1, 2, 0, 1, 1, 8 * 1024 * 1024 + 1, 0))


@pytest.mark.parametrize("wire", [b"HAF", record(1) + record(2, sequence=1, payload=JPEG)[:-1], record(1)])
def test_eof_does_not_accept_partial_record_or_unclosed_session(wire):
    parser = FrameStreamParser()
    parser.feed(wire)
    with pytest.raises(FrameProtocolError):
        parser.finish()


def test_v2_records_carry_a_capture_timestamp_and_v1_records_do_not():
    parser = FrameStreamParser()
    accepted = parser.feed(
        record(1) + record(2, sequence=1, payload=JPEG)
        + record(3) + record(1, epoch=2, version=2)
        + record(2, epoch=2, sequence=1, payload=JPEG, version=2, reserved=12345)
        + record(2, epoch=2, sequence=2, payload=JPEG, version=2, reserved=0)
        + record(3, epoch=2, version=2))
    parser.finish()
    assert [x.capture_ms for x in accepted] == [None, None, None, None, 12345, None, None]


def test_stale_epoch_must_still_have_a_structurally_valid_jpeg():
    parser = FrameStreamParser()
    parser.feed(record(1, epoch=2))
    with pytest.raises(FrameProtocolError):
        parser.feed(record(2, epoch=1, sequence=1, payload=b"bad!"))


def test_sequence_may_skip_dropped_frames_but_not_regress():
    parser = FrameStreamParser()
    accepted = parser.feed(record(1) + record(2, sequence=1, payload=JPEG)
                           + record(2, sequence=17, payload=JPEG) + record(3))
    assert [x.sequence for x in accepted if x.kind == 2] == [1, 17]


def test_maximum_jpeg_is_complete_and_bounded_across_many_chunks():
    from hidagent.uxplay_frames import MAX_JPEG

    payload = b"\xff\xd8" + b"x" * (MAX_JPEG - 4) + b"\xff\xd9"
    parser = FrameStreamParser()
    parser.feed(record(1))
    wire = record(2, sequence=1, payload=payload)
    accepted = []
    for offset in range(0, len(wire), 65536):
        accepted += parser.feed(wire[offset:offset + 65536])
        assert len(parser._buffer) <= MAX_JPEG
    assert len(accepted) == 1 and accepted[0].payload == payload
    parser.feed(record(3))
    parser.finish()

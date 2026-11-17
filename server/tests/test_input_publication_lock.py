"""Publication locks follow nested write guards without blocking emergency release."""

import asyncio
import threading

import pytest
from test_control_input_context import Link

from hidagent.device import Device, DeviceError, input_attempt


def test_nested_guard_inherits_publication_lock_and_accounting():
    lock = threading.RLock()
    with input_attempt(write_lock=lock) as parent:
        with input_attempt(inherit=True) as child:
            assert child.write_lock is lock
            child.started = True
            child.written = True
            assert parent.started and parent.written


def test_nested_different_publication_lock_is_rejected():
    with input_attempt(write_lock=threading.RLock()):
        with pytest.raises(DeviceError, match='来源锁已变化'):
            with input_attempt(inherit=True, write_lock=threading.RLock()):
                pytest.fail('mismatched lock scope admitted')


def test_actual_write_rejects_conflicting_explicit_publication_lock():
    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        try:
            with input_attempt(write_lock=threading.RLock()) as attempt:
                with pytest.raises(DeviceError, match='来源锁已变化'):
                    await device.send_guarded('key a', lambda: None, write_lock=threading.RLock())
                assert not link.written and not attempt.started and not attempt.written
        finally:
            device.close()

    asyncio.run(scenario())


def test_release_bypasses_inherited_guard_and_publication_lock():
    class UnavailableLock:
        def __enter__(self):
            pytest.fail('cleanup must not acquire observation lock')

        def __exit__(self, *args):
            pass

    async def scenario():
        device, link = Device(), Link()
        device.attach(link)
        try:
            with input_attempt(write_lock=UnavailableLock(), before_write=lambda: pytest.fail('old guard')) as attempt:
                await device.send('release')
                assert len(link.written) == 1 and b' release\n' in link.written[0]
                assert not attempt.started and not attempt.written
        finally:
            device.close()

    asyncio.run(scenario())

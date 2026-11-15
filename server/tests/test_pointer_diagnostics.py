import json
import stat

from hidagent.pointer_diagnostics import PointerDiagnostics


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv('HIDAGENT_POINTER_DIAGNOSTICS', raising=False)
    assert PointerDiagnostics.from_env() is None


def test_frames_are_bounded_private_and_new_attempt_replaces_old(tmp_path, monkeypatch):
    directory = tmp_path / 'evidence'
    monkeypatch.setenv('HIDAGENT_POINTER_DIAGNOSTICS', str(directory))
    recorder = PointerDiagnostics.from_env()
    assert recorder is not None
    for number in range(150):
        recorder.capture('baseline' if number == 0 else 'return', bytes([number]), recorder.started_wall)
    recorder.event('result', pointer=[.5, .5])
    frames = list(directory.glob('*.jpg'))
    assert len(frames) == 48
    assert mode(directory) == 0o700
    assert all(mode(path) == 0o600 for path in directory.iterdir())
    assert (directory / 'pointer-frame-00.jpg').read_bytes() == b'\0'
    data = json.loads((directory / 'pointer-attempt.json').read_text())
    assert len(data['frames']) == 48
    assert data['frames'][-1]['sequence'] == 149
    assert data['events'][-1]['pointer'] == [.5, .5]
    directory.joinpath('unrelated.txt').write_text('keep')
    second = PointerDiagnostics.from_env()
    second.capture('baseline', b'new', second.started_wall)
    assert len(list(directory.glob('*.jpg'))) == 1
    assert directory.joinpath('unrelated.txt').read_text() == 'keep'
    assert json.loads((directory / 'pointer-attempt.json').read_text())['events'] == []


def test_rejects_symlink_directory_and_metadata(tmp_path, monkeypatch):
    target = tmp_path / 'target'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv('HIDAGENT_POINTER_DIAGNOSTICS', str(alias))
    assert PointerDiagnostics.from_env() is None
    secret = tmp_path / 'unrelated'
    secret.write_bytes(b'unchanged')
    target.joinpath('pointer-attempt.json').symlink_to(secret)
    monkeypatch.setenv('HIDAGENT_POINTER_DIAGNOSTICS', str(target))
    assert PointerDiagnostics.from_env() is None
    assert secret.read_bytes() == b'unchanged'


def test_extended_probe_keeps_target_and_position_references(tmp_path):
    recorder = PointerDiagnostics(tmp_path / 'evidence')
    for phase in ('baseline', 'target_reference', 'position_reference'):
        recorder.capture(phase, phase.encode())
    for number in range(100):
        recorder.capture('outward_extended' if number < 60 else 'return', bytes([number]))
    records = json.loads((recorder.directory / 'pointer-attempt.json').read_text())['frames']
    assert len(records) == 48
    assert [record['phase'] for record in records[:3]] == ['baseline', 'target_reference', 'position_reference']
    for record in records[:3]:
        assert (recorder.directory / record['file']).read_bytes() == record['phase'].encode()


def test_admission_capture_is_private_bounded_and_preserves_pointer_evidence(tmp_path, monkeypatch):
    directory = tmp_path / 'evidence'
    monkeypatch.setenv('HIDAGENT_POINTER_DIAGNOSTICS', str(directory))
    pointer = PointerDiagnostics.from_env()
    pointer.capture('baseline', b'pointer')
    admission = PointerDiagnostics.from_env(prefix='action-admission')
    admission.capture('original', b'old')
    admission.capture('current', b'new')
    replacement = PointerDiagnostics.from_env(prefix='action-admission')
    replacement.capture('original', b'next')
    assert (directory / 'pointer-frame-00.jpg').read_bytes() == b'pointer'
    assert len(list(directory.glob('action-admission-frame-*.jpg'))) == 1
    assert mode(directory) == 0o700
    assert all(mode(path) == 0o600 for path in directory.iterdir())

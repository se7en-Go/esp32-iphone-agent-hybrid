"""Pure builder validation: no compilers, listeners, dependency changes or downloads."""
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("uxplay_builder", REPO / "scripts/build_uxplay_runtime.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.deps = self.root / "deps"
        self.deps.mkdir()
        (self.deps / "plist.a").write_bytes(b"fixture")
        (self.deps / "crypto.a").write_bytes(b"fixture")
        self.bundle = self.root / "bundle.json"
        self.raw = {"schema": 1, "tools": {name: sys.executable for name in builder.TOOLS},
                    "prefixes": ["deps"], "libraries": {"plist": "deps/plist.a", "crypto": "deps/crypto.a"}}
        self.bundle.write_text(json.dumps(self.raw))
        self.archive = self.root / "upstream.tar.gz"
        self.output = self.root / "new-prefix"

    def archive_with(self, name="root/file.txt", kind=tarfile.REGTYPE):
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = 2
                archive.addfile(member, io.BytesIO(b"ok"))
            else:
                member.linkname = "outside"
                archive.addfile(member)
        return {"archive_sha256": builder.sha256(self.archive), "archive_root": "root"}

    def args(self):
        return SimpleNamespace(archive=self.archive, dependency_bundle=self.bundle,
                               output_prefix=self.output, mode="legacy", gstreamer_smoke=False, jobs=1)

    def test_relative_dependency_manifest_uses_only_explicit_prefixes(self):
        result = builder.load_bundle(self.bundle)
        self.assertEqual(result["libraries"]["plist"], self.deps / "plist.a")

    def test_unknown_manifest_field_rejected(self):
        self.raw["runtime_env"] = "must not be read"
        self.bundle.write_text(json.dumps(self.raw))
        with self.assertRaises(ValueError):
            builder.load_bundle(self.bundle)

    def test_library_outside_bundle_rejected(self):
        (self.root / "outside.a").write_bytes(b"outside")
        self.raw["libraries"]["crypto"] = "outside.a"
        self.bundle.write_text(json.dumps(self.raw))
        with self.assertRaises(ValueError):
            builder.load_bundle(self.bundle)

    def test_archive_hash_failure_before_prefix_or_subprocess(self):
        self.archive.write_bytes(b"wrong archive")
        with (patch.object(builder.subprocess, "run", side_effect=AssertionError("unexpected process")),
              self.assertRaisesRegex(ValueError, "SHA256")):
            builder.build(self.args())
        self.assertFalse(self.output.exists())

    def test_existing_prefix_preserved(self):
        self.archive.write_bytes(b"not needed")
        self.output.mkdir()
        marker = self.output / "live"
        marker.write_text("untouched")
        with self.assertRaisesRegex(ValueError, "already exist"):
            builder.build(self.args())
        self.assertEqual(marker.read_text(), "untouched")

    def test_cannot_build_inside_dependency_prefix(self):
        self.archive.write_bytes(b"not needed")
        args = self.args()
        args.output_prefix = self.deps / "new"
        with self.assertRaisesRegex(ValueError, "outside dependency"):
            builder.build(args)
        self.assertFalse(args.output_prefix.exists())

    def test_verified_regular_archive_extracts(self):
        pin = self.archive_with()
        work = self.root / "work"
        work.mkdir()
        source = builder.unpack_verified(self.archive, work, pin)
        self.assertEqual((source / "file.txt").read_bytes(), b"ok")

    def test_unsafe_archive_members_rejected_before_extraction(self):
        for name, kind in [("root/../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                           ("wrong/file", tarfile.REGTYPE), ("root/link", tarfile.SYMTYPE),
                           ("root/link", tarfile.LNKTYPE), ("root/device", tarfile.CHRTYPE)]:
            with self.subTest(name=name, kind=kind):
                pin = self.archive_with(name, kind)
                work = self.root / "work"
                work.mkdir(exist_ok=True)
                with self.assertRaisesRegex(ValueError, "unsafe entry"):
                    builder.unpack_verified(self.archive, work, pin)
                self.assertEqual(list(work.iterdir()), [])

    def test_checked_in_patch_matches_pin(self):
        pin = json.loads((builder.NATIVE / "upstream.json").read_text())
        self.assertEqual(builder.sha256(builder.NATIVE / "patches" / pin["patch"]), pin["patch_sha256"])


if __name__ == "__main__":
    unittest.main()

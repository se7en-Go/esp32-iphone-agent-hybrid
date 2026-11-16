#!/usr/bin/env python3
"""Offline, opt-in UxPlay build into a NEW prefix; never launch an AirPlay server."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "uxplay"
TOOLS = {"cmake", "pkg_config", "cc", "cxx", "ninja", "git"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXTRACTED = 128 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_bundle(path: Path) -> dict:
    raw = json.loads(path.read_text())
    if (not isinstance(raw, dict) or set(raw) - {"schema", "tools", "prefixes", "libraries", "gstreamer"}
            or raw.get("schema") != 1):
        raise ValueError("unsupported dependency bundle schema")
    if (not isinstance(raw.get("tools"), dict) or not isinstance(raw.get("libraries"), dict)
            or set(raw["tools"]) != TOOLS or set(raw["libraries"]) != {"plist", "crypto"}):
        raise ValueError("dependency bundle must specify all tools and both libraries")

    def resolve(value):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("invalid dependency path")
        return (path.parent / value).resolve(strict=True)

    tools = {key: resolve(value) for key, value in raw["tools"].items()}
    if not all(p.is_file() and os.access(p, os.X_OK) for p in tools.values()):
        raise ValueError("dependency tools must be executable files")
    if not isinstance(raw.get("prefixes"), list) or not raw["prefixes"]:
        raise ValueError("dependency prefixes must be a nonempty list")
    prefixes = [resolve(value) for value in raw["prefixes"]]
    if not all(p.is_dir() for p in prefixes):
        raise ValueError("dependency prefixes must be directories")
    libraries = {key: resolve(value) for key, value in raw["libraries"].items()}
    if not all(p.is_file() and any(under(p, prefix) for prefix in prefixes) for p in libraries.values()):
        raise ValueError("libraries must be files inside the explicit dependency prefixes")
    gst = raw.get("gstreamer", {})
    if gst and set(gst) != {"plugins", "scanner"}:
        raise ValueError("GStreamer settings must specify plugins and scanner")
    gst = {key: resolve(value) for key, value in gst.items()}
    if gst and (not gst["plugins"].is_dir() or not gst["scanner"].is_file()):
        raise ValueError("invalid GStreamer test paths")
    return {"tools": tools, "prefixes": prefixes, "libraries": libraries, "gstreamer": gst}


def unpack_verified(archive: Path, work: Path, pin: dict) -> Path:
    if archive.stat().st_size > MAX_ARCHIVE or sha256(archive) != pin["archive_sha256"]:
        raise ValueError("UxPlay archive does not match the pinned SHA256")
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 5000 or sum(m.size for m in members) > MAX_EXTRACTED:
            raise ValueError("source archive exceeds extraction limits")
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != pin["archive_root"] or not (member.isfile() or member.isdir())):
                raise ValueError("source archive has an unsafe entry")
        for member in members:
            destination = work.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as src, destination.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                destination.chmod(0o644)
    source_dir = work / pin["archive_root"]
    source_dir.rename(work / "source")
    return work / "source"


def build(args) -> Path:
    if os.name != "posix":
        raise ValueError("this offline builder supports Unix; upstream legacy Windows builds remain available")
    pin = json.loads((NATIVE / "upstream.json").read_text())
    archive = args.archive.resolve(strict=True)
    bundle = load_bundle(args.dependency_bundle.resolve(strict=True))
    output = args.output_prefix.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output prefix must not already exist; existing runtimes are never replaced")
    output = output.resolve()
    if any(under(output, prefix) for prefix in bundle["prefixes"]):
        raise ValueError("output prefix must be outside dependency prefixes")
    if archive.stat().st_size > MAX_ARCHIVE or sha256(archive) != pin["archive_sha256"]:
        raise ValueError("UxPlay archive does not match the pinned SHA256")
    patch = NATIVE / "patches" / pin["patch"]
    if sha256(patch) != pin["patch_sha256"]:
        raise ValueError("native patch does not match the pinned SHA256")
    if args.gstreamer_smoke and (args.mode != "framed" or sys.platform != "darwin" or not bundle["gstreamer"]):
        raise ValueError("GStreamer smoke requires framed mode, macOS VideoToolbox and explicit plugin paths")
    output.mkdir(mode=0o700, parents=False)
    work = output / ".build-work"
    work.mkdir(mode=0o700)
    (work / "tmp").mkdir(mode=0o700)
    source = unpack_verified(archive, work, pin)
    tools, prefixes = bundle["tools"], bundle["prefixes"]
    pkg_dirs = [str(p / suffix) for p in prefixes for suffix in ("lib/pkgconfig", "share/pkgconfig")]
    # Upstream's Apple CMake rewrites PKG_CONFIG_PATH. A tiny wrapper keeps all
    # package discovery in the explicitly supplied bundle, not a global brew.
    wrapper_py = work / "pkg_config.py"
    wrapper_py.write_text("import os,sys\nos.environ['PKG_CONFIG_PATH']=" + repr(os.pathsep.join(pkg_dirs))
                          + "\nos.environ['PKG_CONFIG_LIBDIR']=" + repr(os.pathsep.join(pkg_dirs))
                          + "\nos.execv(" + repr(str(tools["pkg_config"])) + ",["
                          + repr(str(tools["pkg_config"])) + "]+sys.argv[1:])\n")
    wrapper = work / "pkg-config"
    wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(wrapper_py))} " '"$@"\n')
    wrapper.chmod(0o700)
    # Never inherit API keys, UxPlay config paths, compiler flags, or live plugin
    # registries. No step starts the network receiver or reads its configuration.
    env = {"PATH": os.pathsep.join(dict.fromkeys([str(p.parent) for p in tools.values()] + ["/usr/bin", "/bin"])),
           "LANG": "C", "LC_ALL": "C", "TMPDIR": str(work / "tmp"),
           "PKG_CONFIG_LIBDIR": os.pathsep.join(pkg_dirs),
           "GST_REGISTRY": str(work / "gst-registry.bin"),
           "GST_PLUGIN_SYSTEM_PATH": ""}
    if bundle["gstreamer"]:
        env["GST_PLUGIN_PATH"] = str(bundle["gstreamer"]["plugins"])
        env["GST_PLUGIN_SCANNER"] = str(bundle["gstreamer"]["scanner"])
    log = work / "build.log"

    def run(command, *, cwd=work, timeout=180):
        with log.open("ab") as stream:
            subprocess.run([str(part) for part in command], cwd=cwd, env=env, stdout=stream,
                           stderr=subprocess.STDOUT, check=True, timeout=timeout)

    run([tools["git"], "apply", "--check", patch], cwd=source)
    run([tools["git"], "apply", patch], cwd=source)
    cache = ["-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_PREFIX=" + str(output),
             "-DCMAKE_C_COMPILER=" + str(tools["cc"]), "-DCMAKE_CXX_COMPILER=" + str(tools["cxx"]),
             "-DCMAKE_MAKE_PROGRAM=" + str(tools["ninja"]), "-DPKG_CONFIG_EXECUTABLE=" + str(wrapper),
             "-DCMAKE_PREFIX_PATH=" + ";".join(map(str, prefixes)), "-DNO_MARCH_NATIVE=ON",
             "-DLIBPLIST=" + str(bundle["libraries"]["plist"]),
             "-DLIBCRYPTO=" + str(bundle["libraries"]["crypto"])]
    if args.mode == "framed":
        cache.append("-DHAFE_NATIVE=ON")  # legacy deliberately exercises CMake's OFF default
    run([tools["cmake"], "-S", source, "-B", work / "build", "-G", "Ninja", *cache])
    run([tools["cmake"], "--build", work / "build", "--parallel", str(args.jobs)])
    run([tools["cmake"], "--install", work / "build"])
    binary = output / "bin/uxplay"
    probe = subprocess.run([binary, "--hafe-protocol"], env=env, capture_output=True, timeout=2, check=False)
    expected = (0, b"HAFE/2\n") if args.mode == "framed" else (2, b"HAFE/0\n")
    if (probe.returncode, probe.stdout) != expected:
        raise RuntimeError("built binary failed the non-network capability probe")
    tested = ["capability probe"]
    if args.mode == "framed":
        shutil.copytree(NATIVE / "tests", work / "tests")
        tests = [("test_hafe", "native-wire.bin")]
        if args.gstreamer_smoke:
            tests.append(("test_gstreamer", "gstreamer-wire.bin"))
        for name, wire in tests:
            flags = []
            if name == "test_gstreamer":
                flags = shlex.split(subprocess.check_output(
                    [wrapper, "--cflags", "--libs", "gstreamer-app-1.0"], env=env, timeout=10, text=True))
            run([tools["cxx"], "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pthread",
                 "-I", source / "renderers", work / f"tests/{name}.cpp",
                 source / "renderers/hafe_stream.cpp", "-o", work / f"tests/{name}", *flags])
            run([work / f"tests/{name}", work / f"tests/{wire}"], timeout=20)
            tested.append(name)
    metadata = {"schema": 1, "upstream": pin, "mode": args.mode, "platform": sys.platform,
                "binary_sha256": sha256(binary), "checks": tested,
                "dependency_sha256": {group: {name: sha256(path) for name, path in bundle[group].items()}
                                      for group in ("tools", "libraries")}}
    (output / "build-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--dependency-bundle", required=True, type=Path, help="explicit schema-1 JSON manifest")
    parser.add_argument("--output-prefix", required=True, type=Path, help="new directory; existing paths are refused")
    parser.add_argument("--mode", choices=("legacy", "framed"), default="legacy")
    parser.add_argument("--gstreamer-smoke", action="store_true", help="offline Mac synthetic H264 decode test")
    parser.add_argument("--jobs", type=int, choices=range(1, 17), default=4)
    args = parser.parse_args()
    try:
        binary = build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"Build failed: {exc}\nInspect the new prefix's .build-work/build.log if it was created.\n")
    print(f"Built and checked {args.mode} candidate: {binary}")
    print("No receiver was started or replaced.")


if __name__ == "__main__":
    main()

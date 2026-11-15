# Optional native AirPlay session frames

This patch adds HAFE/1 session events and JPEG frames to UxPlay 1.73.7. Each
mirroring connection gets a decoder with its own immutable epoch. The receiver
can distinguish a sender reconnect even if its PID stays the same. BEGIN, JPEG
and END share one ordered, bounded pipe.

The native CMake option `HAFE_NATIVE` defaults to **OFF**. Enabling it requires
Unix. Windows keeps the default legacy source path. Both modes were built and
tested on macOS arm64.

## Offline build into a new directory

The builder uses a verified local archive and an explicit dependency manifest to
create a candidate in a fresh output prefix.

```sh
python3 scripts/build_uxplay_runtime.py \
  --archive /path/to/UxPlay-v1.73.7.tar.gz \
  --dependency-bundle /path/to/dependencies.json \
  --output-prefix /path/to/new-candidate \
  --mode framed --gstreamer-smoke
```

`upstream.json` pins the verified upstream archive and this patch by SHA256.
Unknown archives, modified patches, unsafe archive entries, existing output
directories and output inside dependency prefixes are refused. A failed build
retains its new private prefix for diagnosis.

The bundle is a small schema-1 JSON manifest, with paths relative to that manifest
or explicit absolute paths. See `dependencies.example.json`. Its tools and
libraries must already exist. GStreamer, GLib, OpenSSL and libplist must have
their pkg-config descriptions under the declared prefixes. The builder isolates
pkg-config discovery and records selected tool and library hashes in
`build-manifest.json`. The build uses the supplied manifest and its own temporary
environment.

Omit `--mode framed` to test the default OFF build. `--gstreamer-smoke` is an
optional macOS-only synthetic H.264 encode/decode check, using VideoToolbox.
Framed builds always run the standalone native writer harness. The builder's
pure validation suite can be run without native dependencies:

```sh
python3 -m unittest discover -s native/uxplay/tests -p 'test_builder.py'
```

The output prefix contains `bin/uxplay`, upstream licenses and documentation, and
`.build-work` with logs and tests. Keep the declared dependency bundle available
for linked libraries and GStreamer plugins when deploying the candidate.

To select the candidate in the Mac supervisor, set `UXPLAY_RUNTIME` to the new
prefix and `HIDAGENT_UXPLAY_FRAMED=1` in the existing `runtime.env`. Set
`GST_PLUGIN_PATH` and `GST_PLUGIN_SCANNER` explicitly to the dependency bundle
locations used in the build. Set `GST_REGISTRY` to a writable cache file in the
new prefix. The managed receiver checks the selected binary's capability at
startup. Selecting a different binary takes effect when that receiver is
restarted. Reloading the web worker keeps the current receiver and mirror.

## Capability and protocol

`uxplay --hafe-protocol` exits before config parsing, GStreamer UI or networking.
The enabled build prints exactly `HAFE/1\n` and exits with status 0. The default
build prints `HAFE/0\n` and exits with status 2.
Python should require the exact HAFE/1 capability with a two-second timeout.
Then pass a writable inherited pipe FD >= 3 via `HIDAGENT_UXPLAY_FRAMED_FD` and
omit `-vs`. A missing variable selects stock rendering. A malformed value is
fatal in an enabled build.

Each record has a 32-byte network-order `!4sBBHQQII` header:

| Field | Value |
| --- | --- |
| magic / version | `HAFE` / 1 |
| kind | BEGIN=1, JPEG=2, END=3 |
| reserved / flags | zero |
| epoch | starts at 1, strictly increasing per TCP accept |
| sequence | starts at 1 per epoch for JPEG; zero for events |
| payload length | zero for events; JPEG 4 bytes through 8 MiB |

JPEG must have SOI/EOI markers; native output comes from `jpegenc`. END of the old
epoch is serialized before the next BEGIN. Old frame/end callbacks are dropped.
On a write failure or a 250 ms record deadline, the pipe closes. The parent
invalidates state on EOF and reaps the old process before opening a new stream.
BEGIN/END must clear the Python snapshot and advance its generation.

## Evidence limits

Validated on macOS arm64: enabled and default-OFF builds, capability probes,
10,000 retired callbacks, serialized session boundaries, bounded oversized and
blocked-pipe failures, actual synthetic H.264→JPEG through two separate decoder
pipelines, and Python cross-language decoding. Sanitizers passed for the earlier
native writer and renderer prototype. H.265 and rapid reconnection from a real
iPhone remain device acceptance checks.

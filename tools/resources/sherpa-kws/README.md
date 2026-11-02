# Pinned source-only regression fixtures

The seven files under `upstream/` are unchanged copies from sherpa-onnx 2.0.1,
commit `af2d8d81dbfa61c4cea4c568da78f3982edca005`. `upstream.json` records their
sizes and SHA-256 hashes and the complete source archive hash. The upstream
Apache-2.0 [LICENSE](upstream/LICENSE) and original copyright headers are retained.
The archive has no root NOTICE file. No model weights or runtime binaries are vendored.

`confidence-repro.cc.in` is CC8's regression driver. It uses the actual pinned
ContextGraph and injects the actual confidence expression from the decoder,
before and after the source patch. It demonstrates a history/backoff bug and
keeps a no-backoff control. It is not an acoustic model test.

`reset-repro.cc.in` compiles the actual Reset/init/result-reset members and the
automatic-silence branch against minimal state interfaces. It checks encoder
preservation versus explicit lifecycle reset, without loading neural weights.
The evaluated body hashes bind the complete patch, including its extracted reset
helper, to the source used in the isolated native evaluation build. Exported
files additionally carry prominent CC8 modification notices; their full hashes
are reported separately from the evaluated body hashes.

`tools/native_kws.py` verifies this subset and can emit an overlay for a future
native build. A complete build must separately validate the whole upstream
archive and build dependencies; the seven-file check does not certify unlisted
files or build scripts. No overlay is automatically installed or executed.

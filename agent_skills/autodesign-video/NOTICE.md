# Third-party runtime notice

This Skill bundles only package manifests and an exact lockfile. Runtime setup
downloads and installs the following upstream software into a versioned user
cache outside the Skill; their source code, models, browser, voices, and Python
environment are not vendored in this package.

- **HyperFrames 0.7.86** — Copyright its contributors; Apache License 2.0.
  Upstream: <https://github.com/heygen-com/hyperframes>. The exact npm release
  performs structural lint and the final video render.
- **kokoro-onnx 0.5.0** — Copyright 2025 github.com/thewh1teagle; MIT License.
  Upstream: <https://github.com/thewh1teagle/kokoro-onnx>.
- **Kokoro v1.0 model and voice data** — downloaded from the official
  `kokoro-onnx` model-files-v1.0 release and accepted only when their recorded
  SHA-256 digests match the Skill's audited constants. Consult the upstream
  release for model-specific terms.
- **soundfile 0.14.0** — BSD 3-Clause License.
- The resolved Python and npm dependency closures retain their own upstream
  license metadata inside the versioned runtime cache. HyperFrames may install
  a compatible headless Chrome build for local rendering under its upstream
  distribution terms.

The Skill's MIT license does not replace or modify these third-party terms.

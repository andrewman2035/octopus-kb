# Third-party notices

Octopus itself is released under the [MIT License](LICENSE) (Copyright (c) 2026 AndrewMan2035). The packages below
keep their own licenses.

Octopus depends on the open-source packages below. They are not copied into this repository: `uv sync` installs them
from PyPI, each under its own license. This list covers the runtime dependencies in `uv.lock` for macOS and Linux
(development tools such as pytest are not included; three Windows-only packages that never install on the supported
platforms are left out).

Versions are the ones pinned in `uv.lock` for this release. The Linux-only rows were read offline from wheels in a
local cache (some at a neighbouring version of the same package) and should be re-checked against the pinned wheels
before a release. Licenses come from each package's metadata
(`License-Expression`, `License`, classifiers); where those were vague ("BSD", "Dual License", a proprietary
classifier) the package's license file was read. Linux-only packages were read from their Linux wheels. "Direct"
marks the packages Octopus itself depends on in `pyproject.toml`; the others come in through them.

## Packages with copyleft terms

| Package | License | Where | What it means |
|---|---|---|---|
| python-telegram-bot | LGPL-3.0-only | both | Weak copyleft on the library itself. Octopus imports it unmodified and installs it as a separate package, which the LGPL allows under any license for Octopus. Modified copies of the library would have to stay LGPL. |
| tld (through trafilatura → courlan) | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later | both | Choice of licenses; used under MPL-1.1 (file-level copyleft on its own files only). |
| certifi, tqdm | MPL-2.0 (tqdm: MPL-2.0 AND MIT) | both | File-level copyleft on their own files; no effect on Octopus's code. |
| av (PyAV) | BSD-3-Clause; its binary wheels bundle FFmpeg libraries under LGPL-2.1-or-later | Linux | Dynamic libraries inside the wheel; replaceable, used unmodified. |
| opencv-python-headless (through rapidocr) | Apache-2.0; its binary wheels bundle FFmpeg and other libraries under LGPL-2.1 | Linux | Same as above. |
| scipy (through mlx-whisper) | BSD-3-Clause; its macOS wheels bundle libgfortran / libgcc under GPL-3.0-or-later WITH GCC-exception-3.1 | macOS | The GCC runtime library exception allows use from software under any license. |
| numpy | BSD-3-Clause AND others; its wheels bundle the GCC runtime (libgfortran / libgcc, and libquadmath on Linux) under GPL-3.0-or-later WITH GCC-exception-3.1 (libquadmath: LGPL-2.1-or-later) | both | Same as scipy: dynamic runtime libraries, used unmodified. |

No dependency is under the GPL or AGPL without an exception or an alternative. None of these terms conflicts with
releasing Octopus's own code under MIT: the LGPL (python-telegram-bot, the FFmpeg libraries in the PyAV and OpenCV
wheels) covers only those libraries, which Octopus uses unmodified as separately installed, replaceable packages; the
MPL is file-level; and the GCC runtime exception allows use from software under any license.

## Docker image

The `Dockerfile` also installs packages from Debian into the image, notably `ffmpeg` (Debian builds it with GPL
components enabled, so those binaries are under the GPL), `tzdata` and `ca-certificates`. Octopus runs `ffmpeg` as a
separate program (it is not linked into Octopus), so this does not affect the MIT license of Octopus's own code. Anyone who **distributes a built image**
(for example on a container registry) also distributes those GPL binaries and must offer their corresponding source,
for example by pointing to Debian's source packages for the exact versions in the image, and must keep their license
files (Debian ships them under `/usr/share/doc/`). Building the image yourself from this repository raises no such
obligation.

## Other attributions

- The WeChat ClawBot client (`src/octopus/channels/ilink.py`) implements Tencent's iLink bot protocol in Python,
  following the behaviour of Tencent's open-source client `@tencent-weixin/openclaw-weixin` 2.4.10 (npm, MIT License).
  The file carries an attribution block with the upstream notice, `Copyright (C) 2026 Tencent.  All rights
  reserved.`, and the MIT permission notice, taken from the package's LICENSE
  (<https://unpkg.com/@tencent-weixin/openclaw-weixin@2.4.10/LICENSE>: "openclaw-weixin is licensed under the
  MIT."). Use of the iLink service itself is subject to Tencent's terms.
- The Chrome extension's traditional-Chinese messages are generated with OpenCC data (Apache-2.0) through
  `opencc-python-reimplemented`.
- Models downloaded at run time are not part of this repository and have their own licenses: Whisper models (MIT,
  OpenAI; MLX and CTranslate2 conversions from Hugging Face), RapidOCR's bundled PaddleOCR models (Apache-2.0), and
  whatever Ollama models you pull (for example `bge-m3`, MIT).

## All runtime dependencies

| Package | Version | Platforms | License (SPDX) | Direct | Source of the license |
|---|---|---|---|---|---|
| aiohappyeyeballs | 2.7.1 | both | PSF-2.0 |  | package metadata |
| aiohttp | 3.14.5 | both | Apache-2.0 AND MIT |  | package metadata |
| aiosignal | 1.4.0 | both | Apache-2.0 |  | package metadata |
| anyio | 4.15.1 | both | MIT |  | package metadata |
| attrs | 26.1.0 | both | MIT |  | package metadata |
| av | 18.1.0 | Linux | BSD-3-Clause (wheels bundle FFmpeg: LGPL-2.1-or-later) | yes | license file |
| babel | 2.18.0 | both | BSD-3-Clause |  | package metadata |
| beautifulsoup4 | 4.15.0 | both | MIT | yes | package metadata |
| certifi | 2026.7.22 | both | MPL-2.0 |  | package metadata |
| cffi | 2.1.1 | both | MIT-0 |  | package metadata |
| charset-normalizer | 3.5.2 | both | MIT |  | package metadata |
| click | 8.5.0 | both | BSD-3-Clause |  | package metadata |
| colorlog | 6.12.0 | Linux | MIT |  | wheel metadata (Linux) |
| courlan | 1.4.0 | both | Apache-2.0 |  | package metadata |
| cryptography | 50.0.2 | both | Apache-2.0 OR BSD-3-Clause | yes | package metadata |
| ctranslate2 | 4.8.4 | Linux | MIT |  | wheel metadata (Linux) |
| dateparser | 1.4.3 | both | BSD-3-Clause |  | package metadata |
| defusedxml | 0.7.1 | both | PSF-2.0 |  | package metadata |
| discord-py | 2.7.1 | both | MIT | yes | package metadata |
| faster-whisper | 1.2.1 | Linux | MIT | yes | wheel metadata (Linux) |
| feedparser | 6.0.14 | both | BSD-2-Clause | yes | package metadata |
| feedparser-sgmllib | 2.1.0 | both | PSF-2.0 |  | package metadata |
| filelock | 4.1.1 | both | MIT |  | package metadata |
| flatbuffers | 25.12.19 | Linux | Apache-2.0 |  | wheel metadata (Linux) |
| frozenlist | 1.8.0 | both | Apache-2.0 |  | package metadata |
| fsspec | 2026.9.0 | both | BSD-3-Clause |  | package metadata |
| h11 | 0.16.0 | both | MIT |  | package metadata |
| hf-xet | 1.7.0 | both | Apache-2.0 |  | package metadata |
| htmldate | 1.11.0 | both | Apache-2.0 |  | package metadata |
| httpcore | 1.0.9 | both | BSD-3-Clause |  | package metadata |
| httpcore2 | 2.13.1 | both | BSD-3-Clause |  | package metadata |
| httpx | 0.28.1 | both | BSD-3-Clause | yes | package metadata |
| httpx2 | 2.13.1 | both | BSD-3-Clause |  | package metadata |
| huggingface-hub | 2.2.0 | both | Apache-2.0 |  | package metadata |
| idna | 3.20 | both | BSD-3-Clause |  | package metadata |
| jaraco-classes | 3.4.0 | both | MIT |  | license file |
| jaraco-context | 6.1.2 | both | MIT |  | package metadata |
| jaraco-functools | 4.6.0 | both | MIT |  | package metadata |
| jeepney | 0.9.0 | Linux | MIT |  | upstream license |
| jieba | 0.42.1 | both | MIT | yes | package metadata |
| jinja2 | 3.1.6 | macOS | BSD-3-Clause |  | license file |
| justext | 3.0.2 | both | BSD-2-Clause |  | package metadata |
| keyring | 25.7.0 | both | MIT | yes | package metadata |
| lark-oapi | 1.7.4 | both | MIT | yes | package metadata |
| llvmlite | 0.50.0 | macOS | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception |  | package metadata |
| lxml | 6.1.3 | both | BSD-3-Clause | yes | package metadata |
| lxml-html-clean | 0.4.5 | both | BSD-3-Clause |  | package metadata |
| markupsafe | 3.0.4 | macOS | BSD-3-Clause |  | package metadata |
| mlx | 0.32.3 | macOS | MIT |  | package metadata |
| mlx-metal | 0.32.3 | macOS | MIT |  | package metadata |
| mlx-whisper | 0.4.3 | macOS | MIT | yes | package metadata |
| more-itertools | 11.2.0 | both | MIT |  | package metadata |
| mpmath | 1.3.0 | macOS | BSD-3-Clause |  | license file |
| multidict | 7.2.1 | both | Apache-2.0 |  | package metadata |
| networkx | 3.7 | macOS | BSD-3-Clause |  | package metadata |
| numba | 0.68.0 | macOS | BSD-2-Clause |  | license file |
| numpy | 2.5.4 | both | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |  | package metadata |
| ocrmac | 1.0.1 | macOS | MIT | yes | package metadata |
| omegaconf | 2.4.0 | Linux | BSD-3-Clause |  | wheel metadata (Linux) |
| onnxruntime | 1.31.1 | Linux | MIT | yes | wheel metadata (Linux) |
| opencc-python-reimplemented | 0.1.7 | both | Apache-2.0 | yes | license file |
| opencv-python-headless | 5.0.0.93 | Linux | Apache-2.0 (wheels bundle FFmpeg: LGPL-2.1-or-later) |  | license file |
| packaging | 26.3 | both | Apache-2.0 OR BSD-2-Clause |  | package metadata |
| pillow | 12.3.0 | both | MIT-CMU | yes | package metadata |
| propcache | 0.5.4 | both | Apache-2.0 |  | package metadata |
| protobuf | 7.36.2 | Linux | BSD-3-Clause |  | wheel metadata (Linux) |
| pyclipper | 1.4.0 | Linux | MIT |  | wheel metadata (Linux) |
| pycparser | 3.11 | both | BSD-3-Clause |  | package metadata |
| pycryptodome | 3.24.1 | both | BSD-2-Clause AND Unlicense (public domain parts) |  | license file |
| pyobjc-core | 12.2.2 | macOS | MIT |  | package metadata |
| pyobjc-framework-cocoa | 12.2.2 | macOS | MIT |  | package metadata |
| pyobjc-framework-coreml | 12.2.2 | macOS | MIT |  | package metadata |
| pyobjc-framework-quartz | 12.2.2 | macOS | MIT |  | package metadata |
| pyobjc-framework-vision | 12.2.2 | macOS | MIT |  | package metadata |
| pypdf | 6.20.1 | both | BSD-3-Clause | yes | package metadata |
| pypdfium2 | 5.14.0 | both | Apache-2.0 OR BSD-3-Clause (bundled PDFium: BSD-3-Clause, Apache-2.0 and others) | yes | license file |
| python-dateutil | 2.9.0.post0 | both | Apache-2.0 AND BSD-3-Clause |  | license file |
| python-telegram-bot | 22.8 | both | LGPL-3.0-only | yes | package metadata |
| pytz | 2026.5 | both | MIT |  | package metadata |
| pyyaml | 6.0.3 | both | MIT | yes | package metadata |
| qrcode | 8.2 | both | BSD-3-Clause | yes | license file |
| rapidocr | 3.10.0 | Linux | Apache-2.0 | yes | wheel metadata (Linux) |
| regex | 2026.9.29 | both | Apache-2.0 AND CNRI-Python |  | package metadata |
| requests | 2.34.2 | both | Apache-2.0 |  | package metadata |
| requests-toolbelt | 1.0.0 | both | Apache-2.0 |  | package metadata |
| scipy | 1.18.1 | macOS | BSD-3-Clause (wheels bundle libgfortran: GPL-3.0-or-later WITH GCC-exception-3.1) |  | license file |
| secretstorage | 3.5.0 | Linux | BSD-3-Clause |  | upstream license |
| setuptools | 84.0.0 | macOS | MIT |  | package metadata |
| shapely | 2.2.0 | Linux | BSD-3-Clause |  | wheel metadata (Linux) |
| six | 1.17.0 | both | MIT |  | package metadata |
| soupsieve | 3.0.3 | both | MIT |  | package metadata |
| sqlite-vec | 0.1.9 | both | MIT OR Apache-2.0 | yes | package metadata |
| sympy | 1.14.0 | macOS | BSD-3-Clause |  | license file |
| tiktoken | 0.14.0 | macOS | MIT |  | package metadata |
| tld | 0.13.2 | both | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later |  | package metadata |
| tokenizers | 0.23.3 | Linux | Apache-2.0 |  | wheel metadata (Linux) |
| torch | 2.14.1 | macOS | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT |  | package metadata |
| tqdm | 4.70.1 | both | MPL-2.0 AND MIT |  | package metadata |
| trafilatura | 2.3.1 | both | Apache-2.0 | yes | package metadata |
| truststore | 0.10.5 | both | MIT |  | package metadata |
| typing-extensions | 4.16.0 | both | PSF-2.0 |  | package metadata |
| tzlocal | 5.4.4 | both | MIT |  | package metadata |
| urllib3 | 2.8.0 | both | MIT |  | package metadata |
| websockets | 15.0.1 | both | BSD-3-Clause |  | package metadata |
| yarl | 1.25.1 | both | Apache-2.0 |  | package metadata |
| youtube-transcript-api | 1.2.4 | both | MIT | yes | package metadata |
| yt-dlp | 2026.8.19 | both | Unlicense | yes | package metadata |

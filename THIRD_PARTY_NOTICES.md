# Third-Party Notices

Project Brain is released under the MIT License (see [`LICENSE`](LICENSE)).
It builds on third-party software, fonts, container images, models and
hosted services that remain under their own licenses and terms. This file
lists them and the obligations that apply.

Nothing in this repository relicenses a third-party component. Where a
component's license differs from MIT, that license governs the component.

Sections:

1. [Assets committed to this repository](#1-assets-committed-to-this-repository) — redistributed here, license texts included
2. [Python dependencies](#2-python-dependencies) — installed from PyPI, not vendored
3. [Web UI dependencies](#3-web-ui-dependencies) — installed from npm, not vendored
4. [Container images and build-time sources](#4-container-images-and-build-time-sources) — pulled or built at deploy time, not redistributed
5. [Models and hosted AI services](#5-models-and-hosted-ai-services) — downloaded or called at runtime under the provider's terms
6. [Trademarks](#6-trademarks)

---

## 1. Assets committed to this repository

These files are copied into the repository and therefore redistributed with
it. Their license texts are kept next to them.

| Asset | Files | Copyright | License |
|---|---|---|---|
| JetBrains Mono (font) | `apps/web/public/fonts/JetBrainsMono-*.woff2` | Copyright 2020 The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono) | SIL Open Font License 1.1 — [`apps/web/public/fonts/OFL-JetBrainsMono.txt`](apps/web/public/fonts/OFL-JetBrainsMono.txt) |
| Outfit (font) | `apps/web/public/fonts/Outfit-*.woff2` | Copyright 2021 The Outfit Project Authors (https://github.com/Outfitio/Outfit-Fonts) | SIL Open Font License 1.1 — [`apps/web/public/fonts/OFL-Outfit.txt`](apps/web/public/fonts/OFL-Outfit.txt) |
| Feather Icons (SVG geometry of the `layers`, `zap`, `code`, `key` and `flag` glyphs) | `apps/web/components/Icon.tsx` | Copyright (c) 2013-2023 Cole Bemis | MIT — full text below |

The bundled fonts are Latin subsets converted to WOFF2 and are not sold on
their own. Keep their copyright notices and OFL text with redistributed files;
the upstream license governs modification and any reserved font names.

### Feather Icons — MIT License

```
The MIT License (MIT)

Copyright (c) 2013-2023 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. Python dependencies

Declared in `pyproject.toml` and pinned in `requirements.lock` /
`requirements-dev.lock`. They are installed by `pip`, not vendored, and keep
their own license files inside each installed distribution. Most use permissive
licenses. `certifi` and `pathspec` use MPL-2.0, a file-level weak-copyleft
license, and are installed unmodified. Private use is permitted; distributing
their source or executable libraries outside an organization carries the MPL's
notice and source-availability requirements, even for unchanged copies. See
the [Mozilla MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

### Runtime (`requirements.lock`)

| Package | Version | License |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| asyncpg | 0.31.0 | Apache-2.0 |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.142.2 | MIT |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx-sse | 0.4.3 | MIT |
| idna | 3.20 | BSD-3-Clause |
| jinja2 | 3.1.6 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| loguru | 0.7.3 | MIT |
| markdown-it-py | 4.2.0 | MIT |
| markupsafe | 3.0.3 | BSD-3-Clause |
| mcp | 1.29.0 | MIT |
| mdurl | 0.1.2 | MIT |
| neo4j | 6.3.1 | Apache-2.0 AND Python-2.0 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| pgvector | 0.5.0 | MIT |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pyjwt[crypto] | 2.15.1 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytz | 2026.4 | MIT |
| pyyaml | 6.0.3 | MIT |
| redis | 8.1.0 | MIT |
| referencing | 0.37.0 | MIT |
| rich | 15.0.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| shellingham | 1.5.4 | ISC |
| sqlalchemy[asyncio] | 2.1.2 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| typer[all] | 0.27.2 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |

### Development only (`requirements-dev.lock`)

| Package | Version | License |
|---|---|---|
| ast-serialize | 0.11.2 | MIT |
| iniconfig | 2.3.0 | MIT |
| librt | 0.16.0 | MIT |
| mypy | 2.4.0 | MIT |
| mypy-extensions | 1.1.0 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pathspec | 1.1.1 | MPL-2.0 |
| playwright | 1.63.0 | Apache-2.0 |
| pluggy | 1.6.0 | MIT |
| pyee | 13.0.1 | MIT |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| ruff | 0.16.10 | MIT |

### Optional extras

| Extra | Package | License |
|---|---|---|
| `gpu`, `late-interaction-gpu` | `pylate` (pulls PyTorch — BSD-3-Clause, `sentence-transformers` — Apache-2.0, `transformers` — Apache-2.0) | MIT |

---

## 3. Web UI dependencies

`apps/web` (Next.js) — declared in `apps/web/package.json`, pinned in
`apps/web/package-lock.json`, installed by npm, not vendored.

| Package | Version | License |
|---|---|---|
| next | 16.3.8 | MIT |
| react, react-dom | 19.3.0 | MIT |
| typescript (dev) | 5.9.3 | Apache-2.0 |
| @types/node, @types/react, @types/react-dom (dev) | — | MIT |

Notable transitive packages:

- `caniuse-lite` — **CC-BY-4.0**, browser-support data by Alexis Deveria
  (https://caniuse.com), pulled in by Next.js. Attribution is given here.
- `sharp` — Apache-2.0; its optional platform binaries (`@img/sharp-libvips-*`)
  are **LGPL-3.0-or-later** and are dynamically linked prebuilt libraries
  downloaded by npm. They are not committed here and are not modified.
- All other transitive packages are MIT, ISC, Apache-2.0, BSD-3-Clause or 0BSD.

---

## 4. Container images and build-time sources

Referenced by `Dockerfile`, `docker-compose*.yml` and `deploy/`. They are
pulled from their registries (or cloned and built) by whoever deploys; this
repository does not redistribute them. Each is used as a separate,
unmodified service.

| Component | Where | License / terms |
|---|---|---|
| `python:3.11-slim` (Debian base) | `Dockerfile`, `deploy/lfm-colbert-*` | Python: PSF-2.0; Debian packages: their own (mostly GPL/LGPL/BSD) |
| `debian:bookworm-slim` | `deploy/lfm-colbert-cpu` | Debian packages: their own |
| `pgvector/pgvector:pg15` | compose | PostgreSQL License (PostgreSQL and pgvector) |
| `redis:7-alpine` | compose | Redis 7.4+: dual RSALv2 / SSPLv1. Self-hosting as a private backing store is permitted; offering Redis itself as a managed service is not. Earlier 7.x: BSD-3-Clause |
| `neo4j:5.12-community` | compose | GPL-3.0 (Neo4j Community Edition), run as a separate server process; Brain talks to it over the network via the Apache-2.0 `neo4j` driver |
| `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime` | `deploy/lfm-colbert-gpu` | PyTorch: BSD-3-Clause; bundled CUDA/cuDNN: NVIDIA license agreements |
| llama.cpp (cloned and built at image build time) | `deploy/lfm-colbert-cpu` | MIT — https://github.com/ggml-org/llama.cpp |
| nginx | `deploy/nginx` (config only) | BSD-2-Clause |

---

## 5. Models and hosted AI services

No model weights are committed to this repository. Models are downloaded at
runtime, or reached through a provider API with the operator's own key. The
operator is responsible for accepting and following each provider's license
and terms of use.

| Model / service | How it is used | License / terms |
|---|---|---|
| `LiquidAI/LFM2.5-ColBERT-350M` (and `-GGUF`) | Optional late-interaction reranker, downloaded from Hugging Face by the `deploy/lfm-colbert-*` sidecars | **LFM Open License v1.0** — free use, including commercial, for entities under USD 10M annual revenue; larger commercial users need a separate license from Liquid AI. See `docs/runbooks/lfm-colbert-cutover.md` |
| `meta/llama-3.1-70b-instruct`, `meta/llama-3.1-8b-instruct` | Default LLMs in `.env.example`, called through the NVIDIA API | Llama 3.1 Community License (Meta) and NVIDIA API terms. Weights are not distributed here |
| `nvidia/nv-embedcode-7b-v1` | Default embedding model in `.env.example`, called through the NVIDIA API | NVIDIA API / model terms |
| OpenAI, Anthropic, Google, NVIDIA | Configurable LLM providers in `brain/llm`; embedding support varies by adapter (Anthropic embeddings are unsupported). `mock` is local test data | Each provider's terms of service / model license |

---

## 6. Trademarks

Product and company names used in this repository — including Claude and
Anthropic, Codex and OpenAI, Gemini and Google, Cursor, Devin, Kiro, NVIDIA,
Llama and Meta, Liquid AI, Neo4j, Redis, PostgreSQL, Docker, Next.js and
Vercel, Obsidian, GitHub and Hugging Face — are trademarks of their
respective owners. They are used only to identify compatible tools and
services. Project Brain is an independent personal project and is not
affiliated with, sponsored by, or endorsed by any of them.

---

*Inventory generated from `requirements.lock`, `requirements-dev.lock`,
`apps/web/package-lock.json`, the Dockerfiles, the compose files and the
font metadata on 2026-10-02. Regenerate when dependencies change.*

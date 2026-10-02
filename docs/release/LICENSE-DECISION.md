# License / distribution decision

Status: **decided 2026-10-02 — MIT.**

| Item | State |
|---|---|
| Intended distribution | Public, non-commercial portfolio / reference project |
| License | MIT — [`LICENSE`](../../LICENSE), copyright Andrei Damashkevich; matches `pyproject.toml` metadata |
| Third-party notices | [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md) — bundled fonts and icons (license texts included), Python and npm dependencies, container images, models and hosted services, trademarks |
| Repo visibility | Private until the owner flips it |

## Why MIT

- Already declared in `pyproject.toml`; the decision only adds the missing file.
- Every runtime dependency is permissive (MIT / BSD / Apache-2.0 / ISC /
  PSF; MPL-2.0 used unmodified), so MIT creates no compatibility conflict.
- Copyleft components (Neo4j Community GPL-3.0, libvips LGPL-3.0) and
  restricted-terms components (Redis 7.4+ RSALv2/SSPL, n8n Sustainable Use
  License, LFM Open License v1.0) run as separate services or runtime
  downloads and are not redistributed in this repository.

## Keep it true

- A new committed asset (font, icon set, vendored code, image) needs its
  license text and a row in section 1 of `THIRD_PARTY_NOTICES.md`.
- Regenerate the dependency tables when `requirements*.lock` or
  `apps/web/package-lock.json` change.

#!/usr/bin/env bash

set -euo pipefail

: "${CI_COMMIT_TAG:?Release must run for a pushed tag}"

export RELEASE_TAG="$CI_COMMIT_TAG"

export GITHUB_REPOSITORY="$CI_REPO" GH_TOKEN="$GITHUB_TOKEN"

export BRAIN_CI_PROVIDER=woodpecker BRAIN_RELEASE_OUTPUT="$PWD/.release-identity"

command -v gh; command -v syft; command -v docker; command -v python3.12

docker info >/dev/null

python3.12 -m venv .release-venv

export PATH="$PWD/.release-venv/bin:$PATH"

python -m pip install build twine

python -m scripts.release_gate --tag "$RELEASE_TAG"

source "$BRAIN_RELEASE_OUTPUT"

test "$commit_sha" = "$CI_COMMIT_SHA"

test "$(git rev-parse HEAD)" = "$commit_sha"

mkdir -p sbom

python -m pip install --upgrade pip build twine
python -m build
twine check dist/*
python scripts/check_release_archives.py dist/*.whl dist/*.tar.gz
python scripts/check_third_party_notices.py dist/*.whl dist/*.tar.gz
(cd dist && sha256sum *.whl *.tar.gz > SHA256SUMS)


printf '%s' "$GITHUB_TOKEN" | docker login ghcr.io --username "$CI_REPO_OWNER" --password-stdin
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
for kind in api worker web; do
  context=.; dockerfile=Dockerfile
  if [ "$kind" = web ]; then context=apps/web; dockerfile=apps/web/Dockerfile; fi
  image="ghcr.io/perlitten/project-brain-$kind"
  docker build --file "$dockerfile" \
    --build-arg "BRAIN_BUILD_SHA=$commit_sha" --build-arg "BRAIN_BUILD_TIME=$build_time" \
    --build-arg "BRAIN_SOURCE_DIGEST=$source_digest" --build-arg "BRAIN_DEPLOY_DIGEST=$deploy_digest" \
    --build-arg BRAIN_REQUIRE_RELEASE_IDENTITY=true \
    --tag "$image:$RELEASE_TAG" --tag "$image:latest" "$context"
  syft "docker:$image:$RELEASE_TAG" -o "spdx-json=sbom/sbom-$kind.spdx.json"
  python scripts/check_image_sbom.py --kind "$kind" "sbom/sbom-$kind.spdx.json"
  actual=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image:$RELEASE_TAG")
  test "$actual" = "$commit_sha"
  (cd sbom && sha256sum "sbom-$kind.spdx.json" > "sbom-$kind.spdx.json.sha256")
done
syft dir:. --exclude "./.release-venv/**" --exclude "./.git/**" --exclude "./dist/**" -o spdx-json=sbom/sbom-source.spdx.json
(cd sbom && sha256sum sbom-source.spdx.json > sbom-source.spdx.json.sha256)


python scripts/check_release_archives.py dist/*.whl dist/*.tar.gz
(cd dist && sha256sum -c SHA256SUMS)
for manifest in sbom/*.sha256; do (cd sbom && sha256sum -c "$(basename "$manifest")"); done
for kind in api worker web; do python scripts/check_image_sbom.py --kind "$kind" "sbom/sbom-$kind.spdx.json"; done
python - <<'PY'
import hashlib, pathlib
dist = pathlib.Path('dist')
sbom = pathlib.Path('sbom')
wheels = list(dist.glob('*.whl'))
archives = list(dist.glob('*.tar.gz'))
expected_sboms = {'sbom-source.spdx.json', 'sbom-api.spdx.json', 'sbom-worker.spdx.json', 'sbom-web.spdx.json'}
expected_files = expected_sboms | {f'{name}.sha256' for name in expected_sboms}
if len(wheels) != 1 or len(archives) != 1 or {p.name for p in sbom.iterdir()} != expected_files:
    raise SystemExit('Release requires one wheel, one sdist, and four checksummed source/image SBOMs')
paths = sorted(wheels + archives + [sbom / name for name in expected_sboms])
pathlib.Path('SHA256SUMS').write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in paths))
PY


python - <<'PY'
import os, pathlib, re
tag = os.environ["RELEASE_TAG"]
ver = tag.lstrip('v')
text = pathlib.Path("CHANGELOG.md").read_text()
m = re.search(rf"## \[?{re.escape(ver)}\]?.*?(?=\n## |\Z)", text, re.S)
notes = m.group(0).strip().split('\n', 1)[1] if m else '\nSee CHANGELOG.md.'
launch = re.search(r'## \[Unreleased\]\n(.*?)(?=\n## |\Z)', text, re.S)
if ver == '0.9.1' and launch and '### Launch remediation' in launch.group(1):
    notes = launch.group(1).strip() + '\n\n' + notes.strip()
pathlib.Path("release-notes.md").write_text(f"## {tag}\n\n{notes.strip()}\n")
PY


python -m scripts.release_gate --tag "$RELEASE_TAG"

# Publish only after every build, SBOM and checksum has passed. Never rebuild here.
for kind in api worker web; do
  docker push "ghcr.io/perlitten/project-brain-$kind:$RELEASE_TAG"
  docker push "ghcr.io/perlitten/project-brain-$kind:latest"
done
gh release create "$RELEASE_TAG" --repo "$CI_REPO" --verify-tag \
  --title "$RELEASE_TAG" --notes-file release-notes.md \
  dist/*.whl dist/*.tar.gz sbom/*.spdx.json SHA256SUMS


---
title: README
created: '2026-07-31'
updated: '2026-07-31'
status: active
tags:
- type/note
---
# LFM2.5-ColBERT isolated canary

This stack does not join the `brain` Compose network and publishes only on
`127.0.0.1:8089`.  It cannot receive Project Brain traffic until an operator
explicitly performs the shadow-stage network/config steps in the cutover
runbook.

Pinned supply-chain inputs:

- `llama.cpp` commit `7e1e28cae36d41fe7bbe9dae7c9625de6565c063`
- Debian base digest
  `sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818`
- model repository revision `bc240003aba07253e261a8aaf0d2c9683318a967`
- BF16 GGUF SHA-256
  `c21d5cacc004cbc7746dbeeaee496c74b01f0f7bfdef1e1a57570d1744ef871b`

Deploy:

```sh
docker compose build
docker compose create
sh ./fetch-model.sh
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8089/health
```

The model volume is `lfm_colbert_canary_models`.  `docker compose down` keeps
it; do not use `down -v` during rollback.

Stop the canary without touching Project Brain:

```sh
docker compose stop
```

Observe:

```sh
docker stats --no-stream lfm-colbert-canary
curl -fsS http://127.0.0.1:8089/metrics
docker inspect lfm-colbert-canary --format '{{json .State.Health}}'
```

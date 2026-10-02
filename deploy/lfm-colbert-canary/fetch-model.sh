#!/usr/bin/env sh
set -eu

MODEL_FILE=LFM2.5-ColBERT-350M-BF16.gguf
MODEL_REVISION=bc240003aba07253e261a8aaf0d2c9683318a967
MODEL_SHA256=c21d5cacc004cbc7746dbeeaee496c74b01f0f7bfdef1e1a57570d1744ef871b
IMAGE=project-brain/lfm-colbert-canary:llama-7e1e28c
VOLUME=lfm_colbert_canary_models
MODEL_URL="https://huggingface.co/LiquidAI/LFM2.5-ColBERT-350M-GGUF/resolve/${MODEL_REVISION}/${MODEL_FILE}"

docker run --rm --entrypoint /bin/sh -v "${VOLUME}:/models" "${IMAGE}" -c \
  "set -eu
   if [ -f /models/${MODEL_FILE} ]; then
     actual=\$(sha256sum /models/${MODEL_FILE} | cut -d' ' -f1)
     if [ \"\$actual\" = ${MODEL_SHA256} ]; then
       echo model_sha256=\$actual
       exit 0
     fi
   fi
   curl -fL --retry 4 --retry-delay 2 '${MODEL_URL}' -o /models/.${MODEL_FILE}.partial
   actual=\$(sha256sum /models/.${MODEL_FILE}.partial | cut -d' ' -f1)
   test \"\$actual\" = ${MODEL_SHA256}
   mv /models/.${MODEL_FILE}.partial /models/${MODEL_FILE}
   chmod 0444 /models/${MODEL_FILE}
   echo model_sha256=\$actual"

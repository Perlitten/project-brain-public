# eval env — source before any harness run (or export via python os.environ in-process)
export DATABASE_URL="postgresql+asyncpg://postgres:postgres_password@localhost:5433/brain_db_eval"
export DEFAULT_EMBEDDING_PROVIDER=openai_compatible
export EMBEDDING_BASE_URL="http://127.0.0.1:18099/v1"
export EMBEDDING_MODEL="jina-code"
export EMBEDDING_DIMENSION=768
export EMBEDDING_API_KEY="eval-local"
export EMBEDDING_MAX_INPUT_CHARS=8000
export INDEX_EMBEDDING_BATCH_SIZE=96
export INDEX_FILE_CONCURRENCY=6
export INDEX_PROVIDER_CONCURRENCY=8
export DEFAULT_LLM_PROVIDER=mock
export EVAL_DIR="/home/ubuntu/eval_external"
export PYTHONHASHSEED=0

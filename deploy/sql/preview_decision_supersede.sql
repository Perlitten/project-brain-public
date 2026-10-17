-- Read-only preview of what brain/database/migrations.py::_ensure_decision_title_uniqueness
-- will supersede on the first boot of a release that carries it (prod c13d056 -> main).
--
-- Run it against the CURRENT production database BEFORE deploying. It references only
-- columns that exist on c13d056 (no superseded_by_id yet) and changes nothing:
--
--   docker compose --project-directory . -f deploy/docker-compose.prod.yml exec -T postgres \
--     psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     -c 'SET default_transaction_read_only = on' -f - < deploy/sql/preview_decision_supersede.sql
--
-- The scope expression is copied from the migration and tests/test_decision_supersede_preview.py
-- keeps the two in sync. The migration keeps the highest id per (title, scope) and marks every
-- other row status='superseded', superseded_by_id=<winner>; rows keep their content.
-- Expected on a c13d056 database: only NULL vs 'system_global' pairs with the same title collide,
-- because the old unique index already folded title and repo_path case.

-- 1) How many decisions become superseded.
WITH scoped AS (
    SELECT id,
           lower(trim(title)) AS title_key,
           CASE WHEN repo_path IS NULL OR trim(repo_path)='system_global' THEN ''
                WHEN substr(trim(repo_path),2,1)=':' OR substr(trim(repo_path),1,2)='//'
                THEN lower(trim(repo_path)) ELSE trim(repo_path) END AS scope_key
    FROM decisions
)
SELECT count(*) AS would_be_superseded
FROM (SELECT id, MAX(id) OVER (PARTITION BY title_key, scope_key) AS winner FROM scoped) ranked
WHERE id <> winner;

-- 2) Which ones, and which row wins each group (review before deploying).
WITH scoped AS (
    SELECT id, title, repo_path, status,
           lower(trim(title)) AS title_key,
           CASE WHEN repo_path IS NULL OR trim(repo_path)='system_global' THEN ''
                WHEN substr(trim(repo_path),2,1)=':' OR substr(trim(repo_path),1,2)='//'
                THEN lower(trim(repo_path)) ELSE trim(repo_path) END AS scope_key
    FROM decisions
), ranked AS (
    SELECT scoped.*, MAX(id) OVER (PARTITION BY title_key, scope_key) AS winner FROM scoped
)
SELECT id, winner AS superseded_by_id, status AS current_status, title, repo_path
FROM ranked
WHERE id <> winner
ORDER BY winner, id;

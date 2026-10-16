-- Restore fence for team snapshots (see brain/team/restore_fence.py).
--
-- scripts/restore_database.py runs this file after postgres_dump.sql in the
-- same `psql --single-transaction` call, so the restored data and the fence
-- commit together or not at all. pg_dump empties search_path, so every name
-- here is schema-qualified. A snapshot without team members (solo) is left
-- untouched; any other snapshot gets an active fence that only
-- `brain team reconcile` clears. The CREATE TABLE covers dumps taken before
-- the table existed and must match brain.team.models.TeamAccessFence.
SELECT pg_catalog.set_config('brain.restore_fence_reason', :'fence_reason', true);

DO $fence$
BEGIN
    IF pg_catalog.to_regclass('public.team_members') IS NULL THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.team_members) THEN
        RETURN;
    END IF;
    CREATE TABLE IF NOT EXISTS public.team_access_fences (
        id SERIAL NOT NULL,
        reason TEXT NOT NULL,
        fenced_at TIMESTAMP WITH TIME ZONE DEFAULT pg_catalog.now() NOT NULL,
        cleared_at TIMESTAMP WITH TIME ZONE,
        cleared_by TEXT,
        PRIMARY KEY (id)
    );
    INSERT INTO public.team_access_fences (reason)
    VALUES (pg_catalog.current_setting('brain.restore_fence_reason'));
END
$fence$;

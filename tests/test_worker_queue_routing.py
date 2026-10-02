from brain.workers.queue import worker_pool_for_job, worker_pool_prefix


def test_worker_jobs_route_to_independent_v2_pools():
    assert worker_pool_for_job("health_check") == "fast"
    assert worker_pool_for_job("reindex") == "maintenance"
    assert worker_pool_for_job("deep_context") == "deep"
    assert worker_pool_prefix("brain:worker", "deep", enabled=True) == "brain:worker:deep"
    assert worker_pool_prefix("brain:worker", "deep", enabled=False) == "brain:worker"

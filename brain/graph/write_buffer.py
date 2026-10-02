"""Bound relationship memory and send scoped UNWIND writes."""

from brain.config.settings import settings
from brain.workers.runtime import check_job_lease


class GraphWriteBuffer:
    def __init__(self, client):
        self.client = client
        self.rows = []

    async def create_node(self, **kwargs):
        await check_job_lease()
        await self.client.create_node(**kwargs)

    async def create_relationship(self, **kwargs):
        self.rows.append(kwargs)
        if len(self.rows) >= settings.INDEX_GRAPH_BATCH_SIZE:
            await self.flush()

    async def flush(self):
        if self.rows:
            await check_job_lease()
            await self.client.create_relationships(self.rows)
            self.rows.clear()

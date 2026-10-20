"""Wait for authenticated service readiness; an open TCP port is insufficient."""
import asyncio
import os
import time

import asyncpg
import redis
from neo4j import GraphDatabase


def main():
    deadline = time.monotonic() + 180
    while True:
        try:
            async def check_postgres():
                connection = await asyncpg.connect(
                    host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
                    user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
                    database=os.environ["POSTGRES_DB"], timeout=3,
                )
                try:
                    await connection.fetchval("SELECT 1")
                finally:
                    await connection.close()
            asyncio.run(check_postgres())
            with redis.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]),
                             socket_connect_timeout=3, socket_timeout=3) as cache:
                assert cache.ping()
            with GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"],
                                      os.environ["NEO4J_PASSWORD"]), connection_timeout=3) as graph:
                graph.verify_connectivity()
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(f"CI services did not become ready: {type(exc).__name__}") from None
            time.sleep(3)


if __name__ == "__main__":
    main()

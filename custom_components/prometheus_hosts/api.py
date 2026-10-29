"""Minimal read-only Prometheus HTTP API client."""

from __future__ import annotations

import asyncio

import aiohttp


class PrometheusError(Exception):
    """Prometheus could not be reached or returned an error."""


class PrometheusAuthError(PrometheusError):
    """Prometheus rejected the credentials."""


class PrometheusClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        username: str | None,
        password: str | None,
        timeout: int = 20,
    ) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._auth = aiohttp.BasicAuth(username, password or "") if username else None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with self._session.get(
                f"{self._url}{path}", params=params, auth=self._auth, timeout=self._timeout
            ) as resp:
                if resp.status in (401, 403):
                    raise PrometheusAuthError(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise PrometheusError(f"HTTP {resp.status} for {path}")
                body = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise PrometheusError(str(err)) from err
        if body.get("status") != "success":
            raise PrometheusError(body.get("error", "unknown error"))
        return body["data"]

    async def query(self, promql: str) -> list[dict]:
        data = await self._get("/api/v1/query", {"query": promql})
        return data.get("result", [])

    async def alerts(self) -> list[dict]:
        data = await self._get("/api/v1/alerts")
        return data.get("alerts", [])

    async def query_all(self, queries: dict[str, str]) -> dict[str, list[dict]]:
        """Run all queries in parallel; a failing single query yields an empty result."""
        names = list(queries)
        results = await asyncio.gather(
            *(self.query(queries[n]) for n in names), return_exceptions=True
        )
        out: dict[str, list[dict]] = {}
        for name, res in zip(names, results):
            if isinstance(res, PrometheusAuthError):
                raise res
            out[name] = [] if isinstance(res, Exception) else res
        if all(isinstance(r, Exception) for r in results):
            raise PrometheusError(str(results[0]))
        return out

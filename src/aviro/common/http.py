"""Service-to-service HTTP client.

``dependency_timeout target=<service>`` is implemented by asking the callee to
stall (``X-Aviro-Inject-Delay-Ms``), so the caller's *real* timeout fires and
the traces and logs look exactly like a slow dependency. The gateway strips
this header from public traffic.
"""

from typing import Any

import httpx

from aviro.common.faults import FaultEngine

INJECT_DELAY_HEADER = "X-Aviro-Inject-Delay-Ms"


class DependencyError(Exception):
    def __init__(self, service: str, status: int | None, detail: str, *, timeout: bool = False):
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.status = status
        self.detail = detail
        self.timeout = timeout


class ServiceClient:
    def __init__(self, name: str, base_url: str, timeout_s: float, faults: FaultEngine) -> None:
        self.name = name
        self.faults = faults
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s)

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        hang = self.faults.dependency_hang_s(self.name)
        if hang:
            headers[INJECT_DELAY_HEADER] = str(int(hang * 1000))
        try:
            return await self.client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as e:
            raise DependencyError(self.name, None, f"timed out ({type(e).__name__})", timeout=True) from e
        except httpx.HTTPError as e:
            raise DependencyError(self.name, None, f"unreachable ({type(e).__name__})") from e

    async def close(self) -> None:
        await self.client.aclose()

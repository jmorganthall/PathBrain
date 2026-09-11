"""A fault-injecting provider for the write-path tests.

Wraps the mock provider and fails the next N calls the way a real firewall does: a
``ReadTimeout`` (the reconfigure is still running), a ``ConnectError`` (the box is
rebooting), a 502 (its proxy answered before the service did). Every call is counted, so a
test can assert that a timed-out write was NEVER reissued and that a profile switch cost
exactly one reconfigure.
"""
from __future__ import annotations

import httpx

from pathbrain.providers.mock import MockProvider


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://firewall.test/api")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


class FaultyProvider(MockProvider):
    """``fail_next(kind, n)`` makes the next ``n`` write calls raise; ``dark(n)`` makes the
    next ``n`` calls of ANY kind raise ConnectError (a rebooting firewall). ``calls`` is
    every method invoked, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._write_faults: list[BaseException] = []
        self._dark = 0
        self.reconfigures = 0
        # ``land`` = the write reaches the firewall even though the call raises (a timeout
        # on a reconfigure that took) — the case the wrapper must detect by re-reading.
        self.land_on_fault = False

    def fail_next(self, kind: str = "timeout", n: int = 1, *, land: bool = False) -> None:
        exc: BaseException
        if kind == "timeout":
            exc = httpx.ReadTimeout("timed out", request=httpx.Request("POST", "https://firewall.test/api"))
        elif kind == "connect":
            exc = httpx.ConnectError("connection refused", request=httpx.Request("POST", "https://firewall.test/api"))
        elif kind == "502":
            exc = _http_error(502)
        elif kind == "400":
            exc = _http_error(400)
        else:
            raise ValueError(kind)
        self._write_faults.extend([exc] * n)
        self.land_on_fault = land

    def dark(self, n: int) -> None:
        self._dark = n

    def _maybe_dark(self, name: str) -> None:
        self.calls.append(name)
        if self._dark > 0:
            self._dark -= 1
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "https://firewall.test/api"))

    def discover(self):
        self._maybe_dark("discover")
        return super().discover()

    def pipe_states(self):
        self._maybe_dark("pipe_states")
        return super().pipe_states()

    def _write(self, name: str, fn):
        self._maybe_dark(name)
        if self._write_faults:
            exc = self._write_faults.pop(0)
            if self.land_on_fault:
                fn()
                self.reconfigures += 1
            raise exc
        out = fn()
        self.reconfigures += 1
        return out

    def apply(self, changes: dict) -> dict:
        return self._write("apply", lambda: super(FaultyProvider, self).apply(changes))

    def apply_many(self, changes: list[dict]) -> dict:
        def run():
            applied = [MockProvider.apply(self, ch) for ch in changes]
            return {"provider": self.name, "ok": True, "applied": applied, "reconfigures": 1}
        return self._write("apply_many", run)

    def set_pipe_enabled(self, pipe_uuid, enabled: bool) -> dict:
        return self._write("set_pipe_enabled", lambda: super(FaultyProvider, self).set_pipe_enabled(pipe_uuid, enabled))

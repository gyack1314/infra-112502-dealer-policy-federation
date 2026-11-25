"""门店终端与中央规则服务之间的可离线客户端。"""

from __future__ import annotations

from typing import Any, Callable
import json
import urllib.error
import urllib.request

from rule_middleware import RuleMiddleware


OFFLINE_RESULT: dict[str, Any] = {
    "ok": False,
    "status": "offline",
    "decision": "unavailable",
    "can_commit": False,
    "can_redeem": False,
    "consumed": False,
    "reason": "central_unreachable",
    "message": "连不上中央规则服务：本次不能签发或兑现承诺；可查看本机缓存的已知依据。",
}


class CentralUnreachableError(ConnectionError):
    """Raised when a transport cannot reach the central service."""


class OfflineCentralClient:
    """A transport double that always behaves as disconnected."""

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise CentralUnreachableError("simulated offline")


class LocalCentralClient:
    """In-process adapter, useful for tests and single-process deployments."""

    def __init__(self, middleware: RuleMiddleware | None = None) -> None:
        self.middleware = middleware or RuleMiddleware()

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.middleware.handle(payload)


class RemoteCentralClient:
    """Minimal stdlib JSON/HTTP client.

    ``transport`` may be supplied in tests and must raise an exception to
    simulate connectivity failure.  Without an endpoint/transport this client
    is explicitly offline rather than silently using local state.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        timeout: float = 2.0,
    ) -> None:
        self.endpoint = endpoint
        self.transport = transport
        self.timeout = timeout

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(payload)
        if not self.endpoint:
            raise CentralUnreachableError("no central endpoint configured")
        url = self.endpoint.rstrip("/") + "/events"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx/422 are structured business responses, not connectivity loss.
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as parse_exc:
                raise CentralUnreachableError(str(parse_exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CentralUnreachableError(str(exc)) from exc


class StoreTerminal:
    """Store-facing terminal with explicit online/offline semantics."""

    def __init__(
        self,
        central: RuleMiddleware | LocalCentralClient | None = None,
        client: Any | None = None,
        online: bool = True,
        cache: dict[str, Any] | None = None,
    ) -> None:
        if client is not None:
            self.client = client
        elif isinstance(central, RuleMiddleware):
            self.client = LocalCentralClient(central)
        elif central is not None:
            self.client = central
        else:
            self.client = LocalCentralClient(RuleMiddleware())
        self.online = online
        self.cache = cache or {}

    def set_online(self, online: bool) -> None:
        self.online = online

    @classmethod
    def disconnected(cls, cache: dict[str, Any] | None = None) -> "StoreTerminal":
        """Create a terminal whose central connection is explicitly unavailable."""
        return cls(client=OfflineCentralClient(), online=False, cache=cache)

    @staticmethod
    def offline_result() -> dict[str, Any]:
        return dict(OFFLINE_RESULT)

    def _dispatch(self, kind: str, record: dict[str, Any], at: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": kind, "record": record}
        if at is not None:
            payload["at"] = at
        if not self.online:
            return dict(OFFLINE_RESULT)
        try:
            result = self.client.dispatch(payload)
        except (CentralUnreachableError, ConnectionError, TimeoutError, OSError):
            result = dict(OFFLINE_RESULT)
            self.online = False
            return result
        self._update_cache(kind, record, result)
        return result

    def _update_cache(
        self, kind: str, record: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if kind == "inspect":
            view = result.get("view") if isinstance(result, dict) else None
            if isinstance(view, dict):
                key = view.get("promise") or view.get("request_id")
                if key:
                    self.cache[str(key)] = view
        for key_field in ("promise",):
            key = record.get(key_field)
            if key and result.get("promise_view"):
                self.cache[str(key)] = result["promise_view"]

    def query(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self._dispatch("request", record, at)

    def request(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.query(record, at)

    def issue(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self._dispatch("commit", record, at)

    def commit(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self.issue(record, at)

    def redeem(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        return self._dispatch("redeem", record, at)

    def inspect(self, target: str | dict[str, Any], at: int | None = None) -> dict[str, Any]:
        if isinstance(target, str):
            record: dict[str, Any] = {"ref": target}
        else:
            record = target
        result = self._dispatch("inspect", record, at)
        if result.get("status") == "offline" and isinstance(target, str):
            cached = self.cache.get(target)
            if cached is not None:
                result["status"] = "offline_cached_view"
                result["cached_view"] = cached
                result["can_view_cached_basis"] = True
        return result


Store = StoreTerminal
StoreClient = StoreTerminal
OfflineTerminal = StoreTerminal

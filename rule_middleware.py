"""合成矿区场景的管辖感知规则中间件。

只依赖 Python 3.11 标准库。核心对象 :class:`RuleMiddleware` 按事件到达顺序处理
authority / clause / notify_stop / completeness / request / commit / redeem /
inspect 八类消息，并保留可审计的条款版本、承诺窗口和兑现事实。
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable
import json

MESSAGE_KINDS = frozenset(
    {
        "authority",
        "clause",
        "commit",
        "completeness",
        "inspect",
        "notify_stop",
        "redeem",
        "request",
    }
)

ROLE_ALIASES = {
    "regulator": "regulator",
    "reg": "regulator",
    "agency": "regulator",
    "local_agency": "regulator",
    "authority": "regulator",
    "监管": "regulator",
    "监管机构": "regulator",
    "代理机构": "regulator",
    "区域监管": "regulator",
    "equipment": "equipment",
    "tech": "equipment",
    "supplier": "equipment",
    "vendor": "equipment",
    "设备": "equipment",
    "设备方": "equipment",
    "供应方": "equipment",
    "设备供应方": "equipment",
    "commercial": "commercial",
    "sales": "commercial",
    "business": "commercial",
    "biz": "commercial",
    "商业": "commercial",
    "业务": "commercial",
    "商业方": "commercial",
    "业务方": "commercial",
}

KIND_DEFAULT_ROLE = {
    "ban": "regulator",
    "offer": "equipment",
    "exception": "commercial",
}

STOP_FLAG_FIELDS = (
    "stop_fulfillment",
    "stop_existing",
    "stops_existing",
    "stop_promises",
    "stop_commitments",
    "stops_commitments",
    "stop_all",
)


def normalize_role(role: Any) -> str | None:
    if role is None:
        return None
    key = str(role).strip().lower().replace("-", "_").replace(" ", "_")
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    return key


def _is_wildcard(value: Any) -> bool:
    return value is None or value == "*" or value == ""


def value_matches(pattern: Any, value: Any) -> bool:
    """Return whether a clause constraint matches a request value.

    ``None`` / ``"*"`` means unconstrained.  A list request means every
    requested value must be covered; a scalar request may match any pattern
    list entry.
    """
    if _is_wildcard(pattern):
        return True
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(value_matches(pattern, item) for item in value)
    if isinstance(pattern, (list, tuple, set, frozenset)):
        return value in pattern
    return pattern == value


def normalize_credential(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        item = dict(value)
        cred_id = (
            item.get("id")
            or item.get("credential")
            or item.get("credential_id")
            or item.get("type")
            or item.get("name")
        )
        if cred_id is None:
            raise ValueError("凭证对象缺少 id/credential/type/name")
        item["id"] = str(cred_id)
        return item
    return {"id": str(value)}


def credential_id(value: Any) -> str:
    return normalize_credential(value)["id"]


def credential_matches(requirement: Any, provided: Any) -> bool:
    req = normalize_credential(requirement)
    prov = normalize_credential(provided)
    if req["id"] != prov["id"]:
        return False
    # ``type`` is often the human-readable identifier in simple synthetic
    # records and is already reflected in ``id``; do not double-constrain it.
    for field in ("issuer", "line", "role", "jurisdiction"):
        if field in req and req[field] != prov.get(field):
            return False
    return True


def requirement_allows_waiver(requirement: Any) -> bool:
    """A commercial exception may only remove a waivable commercial requirement."""
    req = normalize_credential(requirement)
    if req.get("waivable") is False or req.get("exception_allowed") is False:
        return False
    line = normalize_role(
        req.get("line") or req.get("role") or req.get("jurisdiction")
    )
    if line is not None:
        return line == "commercial"

    cred_id = req["id"]
    if ":" in cred_id:
        prefix, _rest = cred_id.split(":", 1)
        role = normalize_role(prefix)
        if role in ("regulator", "equipment"):
            return False
        if role == "commercial":
            return True
    return True


def parse_related_reference(exception: dict[str, Any]) -> dict[str, Any]:
    raw = (
        exception.get("related")
        or exception.get("related_clause")
        or exception.get("target")
        or exception.get("references")
    )
    ref: dict[str, Any] = {
        "issuer": exception.get("related_issuer")
        or exception.get("clause_issuer")
        or exception.get("target_issuer"),
        "clause": exception.get("related_clause_id"),
        "rev": exception.get("related_rev")
        if exception.get("related_rev") is not None
        else exception.get("related_revision"),
    }
    if isinstance(raw, dict):
        ref["issuer"] = raw.get("issuer", ref["issuer"])
        ref["clause"] = raw.get("clause") or raw.get("id") or ref["clause"]
        ref["rev"] = raw.get("rev", raw.get("revision", ref["rev"]))
        return ref
    if raw is not None:
        text = str(raw)
        if "#" in text:
            text, rev_text = text.rsplit("#", 1)
            try:
                ref["rev"] = int(rev_text)
            except ValueError:
                ref["rev"] = rev_text
        # Support "fit:1" as clause:rev and "issuer:fit:1".
        if ":" in text:
            prefix, maybe_rev = text.rsplit(":", 1)
            if ref["rev"] is None:
                try:
                    ref["rev"] = int(maybe_rev)
                    text = prefix
                except ValueError:
                    pass
        if "/" in text:
            issuer, clause = text.split("/", 1)
            ref["issuer"] = ref["issuer"] or issuer
            text = clause
        elif ":" in text and ref["issuer"] is None:
            issuer, clause = text.split(":", 1)
            ref["issuer"] = issuer
            text = clause
        ref["clause"] = ref["clause"] or text
    return ref


def _revision_equal(left: Any, right: Any) -> bool:
    if left == right:
        return True
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _truthy_stop_flag(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "0", "no", "off"}
    return bool(value)


def clause_key(clause: dict[str, Any]) -> tuple[str, str, str, str, Any]:
    return (
        str(clause["issuer"]),
        str(clause.get("site") or "*"),
        str(clause.get("category") or "*"),
        str(clause.get("clause") or clause.get("id")),
        clause.get("rev", clause.get("revision")),
    )


def family_key_from_key(key: tuple[Any, ...]) -> tuple[str, str, str, str]:
    return key[:4]  # type: ignore[return-value]


def ref_to_key(ref: dict[str, Any]) -> tuple[str, str, str, str, Any]:
    return (
        str(ref.get("issuer")),
        str(ref.get("site") or "*"),
        str(ref.get("category") or "*"),
        str(ref.get("clause") or ref.get("id")),
        ref.get("rev", ref.get("revision")),
    )


def clause_ref(clause: dict[str, Any]) -> dict[str, Any]:
    return {
        "issuer": str(clause["issuer"]),
        "site": clause.get("site", "*"),
        "category": clause.get("category", "*"),
        "clause": clause.get("clause") or clause.get("id"),
        "rev": clause.get("rev", clause.get("revision")),
        "kind": clause.get("kind"),
    }


def _revision_sort_value(rev: Any) -> tuple[int, Any]:
    if isinstance(rev, int) and not isinstance(rev, bool):
        return (1, rev)
    try:
        return (1, int(rev))
    except (TypeError, ValueError):
        return (0, str(rev))


class RuleMiddleware:
    """Event-sourced central rule center.

    The same object can be used directly from Python or through the JSONL
    runner/CLI.  All return values are JSON serializable.
    """

    def __init__(self, require_completeness: bool = True) -> None:
        self.require_completeness = require_completeness
        self.current_at = 0
        self.event_count = 0
        self._lock = RLock()

        self.authority_history: list[dict[str, Any]] = []
        self.authority_latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}

        self.clauses: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.clause_meta: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.family_index: dict[tuple[str, str, str, str], list[tuple[Any, ...]]] = (
            defaultdict(list)
        )
        self.clause_lookup: dict[
            tuple[str, str, str, Any], list[tuple[Any, ...]]
        ] = defaultdict(list)
        self.rejected_clauses: list[dict[str, Any]] = []

        self.stop_notices: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.stop_index: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        self.invalid_notices: list[dict[str, Any]] = []

        self.completeness: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.completeness_history: list[dict[str, Any]] = []

        self.requests: dict[str, dict[str, Any]] = {}
        self.request_keys: dict[str, tuple[str, str]] = {}
        self.promises: dict[str, dict[str, Any]] = {}

        self.events: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Event entry points
    # ------------------------------------------------------------------
    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            raise ValueError("事件必须是对象")
        at = event.get("at")
        if at is None:
            at = self.current_at
        kind = event.get("kind")
        record = event.get("record")
        if type(at) is not int or at < 0:
            raise ValueError("at 必须是非负整数")
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"未登记的消息名称：{kind}")
        if not isinstance(record, dict):
            raise ValueError("record 必须是对象")
        record = dict(record)
        scope = record.get("scope")
        if isinstance(scope, dict):
            for field in ("site", "category", "purpose"):
                if record.get(field) is None and scope.get(field) is not None:
                    record[field] = scope[field]
            if record.get("issuer") is None and scope.get("issuer") is not None:
                record["issuer"] = scope["issuer"]
        event = {**event, "record": record}
        if at < self.current_at:
            raise ValueError("事件时间不能倒退")

        self.event_count += 1
        line = self.event_count
        self.current_at = at
        self.events.append(event)

        handler: Callable[[dict[str, Any], int, int], dict[str, Any]] = {
            "authority": self._handle_authority,
            "clause": self._handle_clause,
            "notify_stop": self._handle_notify_stop,
            "completeness": self._handle_completeness,
            "request": self._handle_request,
            "commit": self._handle_commit,
            "redeem": self._handle_redeem,
            "inspect": self._handle_inspect,
        }[kind]
        result = handler(record, at, line)
        result.setdefault("ok", True)
        result.update({"at": at, "kind": kind, "line": line})
        self.results.append(result)
        return result

    def process_events(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.handle(event) for event in events]

    # Convenience aliases for host applications.
    process_event = handle
    handle_event = handle
    on_message = handle

    def run_jsonl(self, path: str | Path) -> list[dict[str, Any]]:
        path = Path(path)
        results: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    results.append(self.handle(event))
                except (json.JSONDecodeError, ValueError) as exc:
                    error = {
                        "ok": False,
                        "kind": "parse_error",
                        "line": line_no,
                        "error": str(exc),
                    }
                    results.append(error)
                    self.results.append(error)
        return results

    # ------------------------------------------------------------------
    # Authority
    # ------------------------------------------------------------------
    def _handle_authority(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        missing = [
            field
            for field in ("issuer", "role", "site", "category", "revision", "active")
            if field not in record
        ]
        if missing:
            return {
                "ok": False,
                "status": "invalid_authority",
                "errors": [f"缺少字段：{', '.join(missing)}"],
            }
        role = normalize_role(record["role"])
        issuer = str(record["issuer"])
        site = str(record["site"])
        category = str(record["category"])
        key = (issuer, role or str(record["role"]), site, category)
        entry = {
            **record,
            "role": role,
            "received_at": at,
            "line": line,
            "authority_key": key,
        }
        self.authority_history.append(entry)
        self.authority_latest[key] = entry
        return {
            "status": "authority_recorded",
            "issuer": issuer,
            "role": role,
            "site": site,
            "category": category,
            "revision": record["revision"],
            "active": bool(record["active"]),
            "responsibility": "started" if record["active"] else "ended",
        }

    def authority_at(
        self,
        issuer: str,
        role: str | None,
        site: Any,
        category: Any,
        at: int | None = None,
    ) -> dict[str, Any] | None:
        at = self._at(at)
        role_norm = normalize_role(role) if role is not None else None
        site_s = str(site or "*")
        category_s = str(category or "*")

        latest_scope: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.authority_history:
            if entry["received_at"] > at or str(entry["issuer"]) != str(issuer):
                continue
            if role_norm and entry.get("role") != role_norm:
                continue
            if not value_matches(entry.get("site"), site_s):
                continue
            if not value_matches(entry.get("category"), category_s):
                continue
            scope = (str(entry.get("site")), str(entry.get("category")))
            old = latest_scope.get(scope)
            if old is None or (entry["received_at"], entry["line"]) >= (
                old["received_at"],
                old["line"],
            ):
                latest_scope[scope] = entry

        if not latest_scope:
            return None

        def specificity(entry: dict[str, Any]) -> tuple[int, int, int, int]:
            site_score = 0 if _is_wildcard(entry.get("site")) else 2
            cat_score = 0 if _is_wildcard(entry.get("category")) else 1
            return (
                site_score + cat_score,
                site_score,
                entry["received_at"],
                entry["line"],
            )

        best = max(latest_scope.values(), key=specificity)
        return best if best.get("active") else None

    # ------------------------------------------------------------------
    # Clauses
    # ------------------------------------------------------------------
    def _handle_clause(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        missing = [
            field
            for field in ("issuer", "clause", "rev", "kind", "site", "category")
            if field not in record
        ]
        if missing:
            return {
                "ok": False,
                "status": "invalid_clause",
                "errors": [f"缺少字段：{', '.join(missing)}"],
            }
        if record["kind"] not in KIND_DEFAULT_ROLE:
            return {
                "ok": False,
                "status": "invalid_clause",
                "errors": ["kind 必须是 ban、offer 或 exception"],
            }

        key = clause_key(record)
        if key in self.clauses:
            if self.clauses[key] == record:
                return {
                    "status": "clause_duplicate_ignored",
                    "clause": clause_ref(record),
                    "idempotent": True,
                }
            return {
                "ok": False,
                "status": "clause_identity_conflict",
                "clause": clause_ref(record),
                "errors": ["相同 issuer/scope/clause/rev 的条款内容不一致"],
            }

        role = normalize_role(record.get("role") or record.get("line"))
        required_role = role or KIND_DEFAULT_ROLE[record["kind"]]
        auth = self.authority_at(
            str(record["issuer"]),
            required_role,
            record.get("site"),
            record.get("category"),
            at,
        )
        if auth is None:
            rejection = {
                "ok": False,
                "status": "clause_rejected",
                "code": "publisher_without_active_jurisdiction",
                "clause": clause_ref(record),
                "required_role": required_role,
                "received_at": at,
                "record": dict(record),
                "reason": (
                    f"issuer {record['issuer']} 在 "
                    f"{record.get('site')}/{record.get('category')} 的 "
                    f"{required_role} 管辖修订不处于 active 状态"
                ),
            }
            self.rejected_clauses.append(rejection)
            return rejection

        stored = dict(record)
        self.clauses[key] = stored
        self.clause_meta[key] = {
            "received_at": at,
            "line": line,
            "role": required_role,
            "authority": {
                "issuer": auth["issuer"],
                "role": auth.get("role"),
                "revision": auth.get("revision"),
                "received_at": auth["received_at"],
            },
        }
        family = family_key_from_key(key)
        if key not in self.family_index[family]:
            self.family_index[family].append(key)
        lookup_key = (
            str(record.get("site") or "*"),
            str(record.get("category") or "*"),
            str(record.get("clause") or record.get("id")),
            record.get("rev", record.get("revision")),
        )
        self.clause_lookup[lookup_key].append(key)

        revision_stops = self._extract_revision_stops(stored, key, at, line)
        return {
            "status": "clause_published",
            "clause": clause_ref(stored),
            "role": required_role,
            "revision_stops_applied": len(revision_stops),
        }

    def _find_clause_keys(
        self,
        clause_id: str,
        rev: Any = None,
        site: Any = None,
        category: Any = None,
        issuer: str | None = None,
    ) -> list[tuple[Any, ...]]:
        found: list[tuple[Any, ...]] = []
        for key, clause in self.clauses.items():
            k_issuer, k_site, k_category, k_clause, k_rev = key
            if k_clause != str(clause_id):
                continue
            if rev is not None and not _revision_equal(k_rev, rev):
                continue
            if issuer is not None and k_issuer != str(issuer):
                continue
            if site is not None and not value_matches(k_site, str(site)):
                continue
            if category is not None and not value_matches(k_category, str(category)):
                continue
            found.append(key)
        return found

    def current_revision_key(
        self, family: tuple[str, str, str, str], at: int
    ) -> tuple[Any, ...] | None:
        versions = [
            key
            for key in self.family_index.get(family, [])
            if self.clause_meta[key]["received_at"] <= at
        ]
        if not versions:
            return None
        return max(
            versions,
            key=lambda key: (
                _revision_sort_value(key[4]),
                self.clause_meta[key]["received_at"],
                self.clause_meta[key]["line"],
            ),
        )

    # ------------------------------------------------------------------
    # Stop notices and explicit revision stops
    # ------------------------------------------------------------------
    def _stop_is_explicit_in_revision(self, clause: dict[str, Any]) -> bool:
        if any(_truthy_stop_flag(clause.get(field)) for field in STOP_FLAG_FIELDS):
            return True
        stop = clause.get("stop")
        if _truthy_stop_flag(stop):
            if not isinstance(stop, str) or stop.strip().lower() in {
                "fulfillment",
                "existing",
                "all",
                "true",
                "yes",
            }:
                return True
        if isinstance(stop, dict) and (
            stop.get("fulfillment") is True
            or _truthy_stop_flag(stop.get("existing"))
            or _truthy_stop_flag(stop.get("stop_fulfillment"))
        ):
            return True
        effect = str(clause.get("effect") or "").lower()
        fulfillment = str(clause.get("fulfillment") or "").lower()
        return (
            effect in {"stop", "stop_fulfillment", "stop_existing"}
            or fulfillment == "stop"
        )

    def _extract_revision_stops(
        self,
        clause: dict[str, Any],
        source_key: tuple[Any, ...],
        at: int,
        line: int,
    ) -> list[dict[str, Any]]:
        if not self._stop_is_explicit_in_revision(clause):
            return []

        directive: dict[str, Any] = {}
        for field in ("stop", *STOP_FLAG_FIELDS):
            value = clause.get(field)
            if isinstance(value, dict):
                directive = value
                break

        targets: list[tuple[Any, ...]] = []
        raw_targets = (
            clause.get("stop_targets")
            or clause.get("targets")
            or directive.get("targets")
            or directive.get("stop_targets")
        )
        if isinstance(raw_targets, dict):
            raw_targets = [raw_targets]
        if isinstance(raw_targets, list) and raw_targets:
            for raw in raw_targets:
                if isinstance(raw, dict):
                    target_ref = {
                        "issuer": raw.get("issuer", source_key[0]),
                        "site": raw.get("site", source_key[1]),
                        "category": raw.get("category", source_key[2]),
                        "clause": raw.get("clause") or raw.get("id") or source_key[3],
                        "rev": raw.get("rev", raw.get("revision")),
                    }
                    target_key = ref_to_key(target_ref)
                    if target_key in self.clauses:
                        targets.append(target_key)
        else:
            revs = (
                clause.get("stop_revs")
                or clause.get("stop_revisions")
                or directive.get("revs")
                or directive.get("revisions")
            )
            family = family_key_from_key(source_key)
            for key in self.family_index.get(family, []):
                if key == source_key:
                    continue
                if isinstance(revs, list) and not any(
                    _revision_equal(key[4], wanted) for wanted in revs
                ):
                    continue
                targets.append(key)

        stops: list[dict[str, Any]] = []
        reason = (
            clause.get("stop_reason")
            or clause.get("reason")
            or directive.get("reason")
            or "条款修订包含明确停止兑现"
        )
        for target_key in targets:
            stop = {
                "id": f"revision-stop:{line}:{len(self.stops) + 1}",
                "kind": "revision_stop",
                "issuer": source_key[0],
                "target_issuer": target_key[0],
                "site": target_key[1],
                "category": target_key[2],
                "clause": target_key[3],
                "rev": target_key[4],
                "source_clause": clause_ref(clause),
                "reason": reason,
                "effective_at": at,
                "line": line,
            }
            self.stops.append(stop)
            self.stop_index[target_key].append(stop)
            self._apply_stop_to_promises(target_key, stop)
            stops.append(stop)
        return stops

    def _handle_notify_stop(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        record = dict(record)
        target = record.get("target")
        if isinstance(target, dict):
            record.setdefault("clause", target.get("clause") or target.get("id"))
            record.setdefault("rev", target.get("rev", target.get("revision")))
            record.setdefault(
                "clause_issuer", target.get("issuer") or target.get("clause_issuer")
            )
            record.setdefault("site", target.get("site"))
            record.setdefault("category", target.get("category"))
        missing = [field for field in ("issuer", "clause", "rev") if field not in record]
        if missing:
            return {
                "ok": False,
                "status": "invalid_notice",
                "errors": [f"缺少字段：{', '.join(missing)}"],
            }

        notice_issuer = str(record["issuer"])
        clause_id = str(record["clause"])
        rev = record["rev"]
        target_issuer = record.get("clause_issuer") or record.get("target_issuer")
        keys = self._find_clause_keys(
            clause_id,
            rev,
            site=record.get("site"),
            category=record.get("category"),
            issuer=str(target_issuer) if target_issuer is not None else None,
        )
        if not keys:
            invalid = {
                "ok": False,
                "status": "notice_rejected",
                "code": "target_clause_not_found",
                "received_at": at,
                "record": dict(record),
                "reason": "停止兑现通知没有指向已发布的条款版本",
            }
            self.invalid_notices.append(invalid)
            return invalid

        applied: list[dict[str, Any]] = []
        unauthorized: list[dict[str, Any]] = []
        for key in keys:
            target_clause = self.clauses[key]
            meta = self.clause_meta[key]
            if key[0] == notice_issuer:
                auth = self.authority_at(
                    notice_issuer, meta["role"], key[1], key[2], at
                )
            else:
                auth = self.authority_at(
                    notice_issuer, "regulator", key[1], key[2], at
                )
            if auth is None:
                unauthorized.append(clause_ref(target_clause))
                continue

            already = any(
                s.get("kind") == "notify_stop"
                and s.get("notice_line") == line
                and s.get("target_key") == key
                for s in self.stop_index[key]
            )
            if already:
                continue
            stop = {
                "id": f"stop:{line}:{key[3]}:{key[4]}",
                "kind": "notify_stop",
                "issuer": notice_issuer,
                "stopping_authority_revision": auth.get("revision"),
                "target_issuer": key[0],
                "target_key": key,
                "site": key[1],
                "category": key[2],
                "clause": key[3],
                "rev": key[4],
                "reason": record.get("reason", ""),
                "effective_at": at,
                "notice_line": line,
            }
            self.stop_notices.append(stop)
            self.stops.append(stop)
            self.stop_index[key].append(stop)
            self._apply_stop_to_promises(key, stop)
            applied.append(stop)

        if not applied:
            result = {
                "ok": False,
                "status": "notice_rejected",
                "code": "not_a_valid_jurisdiction",
                "targets": [clause_ref(self.clauses[key]) for key in keys],
                "unauthorized_targets": unauthorized,
                "reason": "通知签发方对目标条款没有停止兑现管辖权",
            }
            self.invalid_notices.append({"received_at": at, "record": record, **result})
            return result

        return {
            "status": "stop_registered",
            "stops": [self._public_stop(s) for s in applied],
            "unauthorized_targets": unauthorized,
        }

    def _public_stop(self, stop: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in stop.items() if k != "target_key"}

    def _active_stop(
        self, key: tuple[Any, ...], at: int
    ) -> dict[str, Any] | None:
        active = [s for s in self.stop_index.get(key, []) if s["effective_at"] <= at]
        return max(active, key=lambda s: (s["effective_at"], s.get("line", 0))) if active else None

    def _apply_stop_to_promises(
        self, key: tuple[Any, ...], stop: dict[str, Any]
    ) -> None:
        for promise in self.promises.values():
            if key not in {ref_to_key(ref) for ref in promise.get("basis_refs", [])}:
                continue
            public_stop = self._public_stop(stop)
            if any(s.get("id") == stop.get("id") for s in promise.get("stops", [])):
                continue
            promise.setdefault("stops", []).append(public_stop)
            if promise.get("redemption"):
                promise["review_required"] = True
                promise["review"] = {
                    "required": True,
                    "assignee_store": promise["redemption"].get("store", promise.get("store")),
                    "reason": stop.get("reason") or "交付后收到有效停止兑现通知",
                    "opened_at": stop["effective_at"],
                    "status": "open",
                }
                promise.setdefault("post_delivery_stops", []).append(public_stop)
                promise.setdefault("timeline", []).append(
                    {
                        "at": stop["effective_at"],
                        "event": "post_delivery_stop_received",
                        "stop": public_stop,
                        "note": "交付事实保持不变，要求门店复核",
                    }
                )
            else:
                if promise.get("status") != "expired":
                    promise["status"] = "stopped"
                promise.setdefault("timeline", []).append(
                    {
                        "at": stop["effective_at"],
                        "event": "stop_fulfillment",
                        "stop": public_stop,
                    }
                )

    # ------------------------------------------------------------------
    # Completeness
    # ------------------------------------------------------------------
    def _handle_completeness(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        missing = [field for field in ("store", "site", "category", "complete") if field not in record]
        if missing:
            return {
                "ok": False,
                "status": "invalid_completeness",
                "errors": [f"缺少字段：{', '.join(missing)}"],
            }
        key = (str(record["store"]), str(record["site"]), str(record["category"]))
        entry = {**record, "received_at": at, "line": line}
        self.completeness[key] = entry
        self.completeness_history.append(entry)
        return {
            "status": "completeness_recorded",
            "store": key[0],
            "site": key[1],
            "category": key[2],
            "complete": bool(record["complete"]),
            "cursors": record.get("cursors"),
        }

    def completeness_at(
        self, store: str, site: Any, category: Any, at: int | None = None
    ) -> dict[str, Any] | None:
        at = self._at(at)
        key = (str(store), str(site or "*"), str(category or "*"))
        latest = None
        for entry in self.completeness_history:
            entry_key = (entry["store"], entry["site"], entry["category"])
            if entry_key == key and entry["received_at"] <= at:
                if latest is None or (entry["received_at"], entry["line"]) >= (
                    latest["received_at"],
                    latest["line"],
                ):
                    latest = entry
        if latest is not None:
            return latest
        current = self.completeness.get(key)
        if current and current["received_at"] <= at:
            return current
        return None

    def _completeness_ok(
        self, store: str | None, site: Any, category: Any, at: int
    ) -> tuple[bool, dict[str, Any] | None]:
        if not self.require_completeness:
            return True, None
        if store is None:
            return False, None
        state = self.completeness_at(store, site, category, at)
        return bool(state and state.get("complete") is True), state

    # ------------------------------------------------------------------
    # Requests and evaluation
    # ------------------------------------------------------------------
    def _at(self, at: int | None) -> int:
        return self.current_at if at is None else at

    def _handle_request(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        request_id = record.get("id")
        store = record.get("store")
        if request_id is None or store is None:
            return {
                "ok": False,
                "status": "invalid_request",
                "errors": ["request 必须包含 store 和 id"],
            }
        store = str(store)
        request_id = str(request_id)
        map_key = (store, request_id)
        if request_id in self.requests:
            existing = self.requests[request_id]
            if existing["map_key"] != map_key or existing["raw"] != record:
                return {
                    "ok": False,
                    "status": "request_id_conflict",
                    "request": request_id,
                    "errors": ["request id 已被不同请求占用"],
                }
            evaluation = self.evaluate_request(existing["normalized"], at=at)
            return self._request_result(
                evaluation,
                request_id,
                receipt_status="request_duplicate_ignored",
                idempotent=True,
            )

        normalized = self.normalize_request(record)
        stored = {
            "id": request_id,
            "store": store,
            "map_key": map_key,
            "raw": dict(record),
            "normalized": normalized,
            "received_at": at,
            "line": line,
            "evaluations": [],
        }
        self.requests[request_id] = stored
        self.request_keys[request_id] = map_key
        evaluation = self.evaluate_request(normalized, at=at)
        stored["evaluations"].append({"at": at, "evaluation": evaluation})
        return self._request_result(
            evaluation, request_id, receipt_status="request_recorded"
        )

    @staticmethod
    def _request_result(
        evaluation: dict[str, Any], request_id: str, receipt_status: str, **extra: Any
    ) -> dict[str, Any]:
        flat_keys = (
            "decision",
            "verdict",
            "approved",
            "can_commit",
            "completeness_ok",
            "completeness",
            "missing",
            "missing_credentials",
            "missing_conditions",
            "conflicts",
            "bans",
            "basis",
            "reapply_conditions",
        )
        result = {key: evaluation.get(key) for key in flat_keys}
        result.update(
            {
                # Top-level status is the rule decision; receipt_status keeps
                # the event-sourcing acknowledgement name.
                "status": evaluation["status"],
                "receipt_status": receipt_status,
                "request": request_id,
                "evaluation": evaluation,
            }
        )
        result.update(extra)
        return result

    @staticmethod
    def normalize_request(record: dict[str, Any]) -> dict[str, Any]:
        battery = record.get("battery")
        if battery is None:
            battery = record.get("part", record.get("parts"))
        raw_credentials = record.get("credentials") or []
        if not isinstance(raw_credentials, list):
            raw_credentials = [raw_credentials]
        credentials = [normalize_credential(item) for item in raw_credentials]
        return {
            "store": str(record["store"]) if record.get("store") is not None else None,
            "id": str(record["id"]) if record.get("id") is not None else None,
            "site": record.get("site"),
            "category": record.get("category"),
            "purpose": record.get("purpose"),
            "battery": battery,
            "model": record.get("model") or record.get("device_model"),
            "credentials": credentials,
            "facts": record.get("facts")
            if isinstance(record.get("facts"), dict)
            else record.get("attributes")
            if isinstance(record.get("attributes"), dict)
            else {},
            "raw": dict(record),
        }

    def _clause_matches_request(
        self, clause: dict[str, Any], req: dict[str, Any]
    ) -> tuple[bool, str | None]:
        for field in ("site", "category", "purpose"):
            if field in clause and not value_matches(clause.get(field), req.get(field)):
                return False, field
        if "battery" in clause and not value_matches(
            clause.get("battery"), req.get("battery")
        ):
            return False, "battery"
        if "model" in clause and not value_matches(clause.get("model"), req.get("model")):
            return False, "model"
        if "device_model" in clause and not value_matches(
            clause.get("device_model"), req.get("model")
        ):
            return False, "device_model"
        if "part" in clause and not value_matches(
            clause.get("part"), req.get("raw", {}).get("part", req.get("battery"))
        ):
            return False, "part"
        return True, None

    def _clause_expired(self, clause: dict[str, Any], at: int) -> bool:
        until = clause.get("until")
        return until is not None and at > until

    def evaluate_request(
        self, request: str | dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        at = self._at(at)
        if isinstance(request, str):
            if request not in self.requests:
                return {
                    "ok": False,
                    "status": "unknown_request",
                    "decision": "unavailable",
                    "can_commit": False,
                    "errors": [f"unknown request {request}"],
                }
            req = self.requests[request]["normalized"]
        else:
            req = self.normalize_request(request)

        complete, completeness_state = self._completeness_ok(
            req.get("store"), req.get("site"), req.get("category"), at
        )

        current_keys = set()
        for family in self.family_index:
            current = self.current_revision_key(family, at)
            if current is not None:
                current_keys.add(current)

        matching: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        nonmatching_scope: list[dict[str, Any]] = []
        for key, clause in self.clauses.items():
            meta = self.clause_meta[key]
            if meta["received_at"] > at:
                continue
            matches, mismatch_field = self._clause_matches_request(clause, req)
            if matches:
                matching.append((key, clause))
            elif str(clause.get("site") or "*") == str(req.get("site") or "*"):
                nonmatching_scope.append(
                    {**clause_ref(clause), "mismatch_field": mismatch_field}
                )

        bans: list[dict[str, Any]] = []
        offers: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []
        unavailable_clauses: list[dict[str, Any]] = []

        for key, clause in matching:
            stop = self._active_stop(key, at)
            expired = self._clause_expired(clause, at)
            is_current = key in current_keys
            base = {
                "clause": clause_ref(clause),
                "current_revision": is_current,
                "expired": expired,
                "stop": self._public_stop(stop) if stop else None,
            }
            if not is_current:
                base["status"] = "superseded"
                unavailable_clauses.append(base)
                continue
            if stop:
                base["status"] = "stopped"
                unavailable_clauses.append(base)
                if clause.get("kind") == "ban":
                    bans.append(
                        {
                            "type": "ban_stopped",
                            "clause": clause_ref(clause),
                            "stop": self._public_stop(stop),
                        }
                    )
                continue
            if expired:
                base["status"] = "expired"
                unavailable_clauses.append(base)
                continue

            if clause.get("kind") == "ban":
                lift = self._ban_lift(clause, req, at)
                if lift.get("lifted"):
                    base["status"] = "ban_lifted"
                    unavailable_clauses.append({**base, "lift": lift})
                else:
                    bans.append(
                        {
                            "type": "hard_ban",
                            "clause": clause_ref(clause),
                            "reason": clause.get("reason")
                            or clause.get("message")
                            or "有效监管禁止交付",
                            "lift": lift,
                        }
                    )
            elif clause.get("kind") == "offer":
                offers.append(self._evaluate_offer(key, clause, req, matching, at))
            elif clause.get("kind") == "exception":
                exceptions.append(self._exception_summary(key, clause, at))

        satisfied_offers = [offer for offer in offers if offer["satisfied"]]
        all_missing_credentials: list[dict[str, Any]] = []
        all_missing_conditions: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = list(bans)

        for offer in offers:
            for cred in offer["missing_credentials"]:
                entry = {
                    "type": "credential",
                    "id": cred["id"],
                    "clause": offer["clause"],
                }
                if entry not in all_missing_credentials:
                    all_missing_credentials.append(entry)
            for condition in offer.get("missing_conditions", []):
                all_missing_conditions.append(
                    {"type": "condition", "clause": offer["clause"], **condition}
                )
            conflicts.extend(offer.get("waiver_conflicts", []))

        # If one complete allow proof exists, gaps in alternative offers are
        # not blockers.  Without any proof, every offer's gaps help the store
        # tell the customer what to supply next.
        if satisfied_offers:
            missing_credentials: list[dict[str, Any]] = []
            missing_conditions: list[dict[str, Any]] = []
            alternative_missing_credentials = all_missing_credentials
            alternative_missing_conditions = all_missing_conditions
        else:
            missing_credentials = all_missing_credentials
            missing_conditions = all_missing_conditions
            alternative_missing_credentials = []
            alternative_missing_conditions = []

        proofs = [self._build_proof(offer) for offer in satisfied_offers]
        primary_proof = proofs[0] if proofs else None

        missing: list[dict[str, Any]] = []
        if not complete:
            missing.append(
                {
                    "type": "completeness",
                    "store": req.get("store"),
                    "site": req.get("site"),
                    "category": req.get("category"),
                    "reported": bool(completeness_state),
                    "complete": False,
                    "cursors": (completeness_state or {}).get("cursors"),
                    "message": "该区域分支尚不能证明监管、设备、商业三线资料齐备",
                }
            )
        missing.extend(missing_credentials)
        missing.extend(missing_conditions)

        active_authorities = self._applicable_authority_summary(req, at)

        rule_decision = "allow"
        if bans:
            rule_decision = "deny"
        elif satisfied_offers:
            rule_decision = "allow"
        elif offers:
            rule_decision = "needs_info"
        else:
            rule_decision = "no_basis"

        if bans:
            decision = "deny"
            status = "banned"
        elif satisfied_offers and complete:
            decision = "allow"
            status = "allowed"
        elif satisfied_offers and not complete:
            decision = "hold"
            status = "incomplete"
        elif offers:
            decision = "needs_info"
            status = "missing_information"
        elif not complete:
            decision = "hold"
            status = "incomplete"
        else:
            decision = "deny"
            status = "no_applicable_offer"

        if not complete and decision == "allow":
            decision = "hold"
            status = "incomplete"

        reapply = self._reapply_conditions(
            decision, missing_credentials, missing_conditions, bans, completeness_state
        )
        return {
            "ok": True,
            "evaluated_at": at,
            "decision": decision,
            "status": status,
            "rule_decision": rule_decision,
            "can_commit": decision == "allow",
            "approved": decision == "allow",
            "verdict": {
                "allow": "allow",
                "deny": "reject",
                "needs_info": "needs_information",
                "hold": "hold",
            }.get(decision, decision),
            "completeness_ok": complete,
            "completeness": self._public_completeness(completeness_state),
            "primary_proof": primary_proof,
            "proofs": proofs,
            "basis": [
                {
                    "role": "offer",
                    **primary_proof["offer"],
                    "waived": primary_proof["exceptions"],
                }
            ]
            if primary_proof
            else [],
            "satisfied_offers": [o["clause"] for o in satisfied_offers],
            "offers": offers,
            "bans": bans,
            "exceptions": exceptions,
            "unavailable_clauses": unavailable_clauses,
            "known_scope_clauses": nonmatching_scope,
            "missing": missing,
            "missing_credentials": missing_credentials,
            "missing_conditions": missing_conditions,
            "alternative_missing_credentials": alternative_missing_credentials,
            "alternative_missing_conditions": alternative_missing_conditions,
            "conflicts": conflicts,
            "authorities": active_authorities,
            "reapply_conditions": reapply,
        }

    def _public_completeness(self, state: dict[str, Any] | None) -> dict[str, Any] | None:
        if state is None:
            return None
        return {
            "store": state.get("store"),
            "site": state.get("site"),
            "category": state.get("category"),
            "complete": state.get("complete"),
            "cursors": state.get("cursors"),
            "at": state.get("received_at"),
        }

    def _applicable_authority_summary(
        self, req: dict[str, Any], at: int
    ) -> list[dict[str, Any]]:
        result = []
        for entry in self.authority_history:
            if entry["received_at"] > at:
                continue
            if not value_matches(entry.get("site"), req.get("site")):
                continue
            if not value_matches(entry.get("category"), req.get("category")):
                continue
            # Latest state per exact authority key only.
            latest = self.authority_latest.get(entry["authority_key"])
            if latest is not entry:
                continue
            result.append(
                {
                    "issuer": entry["issuer"],
                    "role": entry.get("role"),
                    "site": entry.get("site"),
                    "category": entry.get("category"),
                    "revision": entry.get("revision"),
                    "active": bool(entry.get("active")),
                    "at": entry.get("received_at"),
                    "required_by_completeness_line": entry.get("role")
                    in {"regulator", "equipment", "commercial"},
                }
            )
        return result

    def _ban_lift(
        self, ban: dict[str, Any], req: dict[str, Any], at: int
    ) -> dict[str, Any]:
        lift_raw = ban.get("lift") or ban.get("lifts") or {}
        if not isinstance(lift_raw, dict):
            lift_raw = {}
        lifts_at = ban.get("lifts_at", ban.get("lift_at", ban.get("unban_at")))
        if lifts_at is None:
            lifts_at = lift_raw.get("at") or lift_raw.get("lifts_at")
        creds = (
            ban.get("lift_credentials")
            or ban.get("lifted_by_credentials")
            or lift_raw.get("credentials")
            or []
        )
        if not isinstance(creds, list):
            creds = [creds]

        provided = req.get("credentials", [])
        missing_creds: list[dict[str, Any]] = []
        satisfied_creds: list[dict[str, Any]] = []
        for requirement in creds:
            match = next(
                (p for p in provided if credential_matches(requirement, p)), None
            )
            if match is None:
                missing_creds.append(normalize_credential(requirement))
            else:
                satisfied_creds.append(match)

        unknown_keys = [
            key
            for key in lift_raw
            if key not in {"at", "credentials", "reason", "description"}
        ]
        time_lifted = lifts_at is not None and at >= lifts_at
        creds_lifted = bool(creds) and not missing_creds and not unknown_keys
        no_unknown = not unknown_keys
        lifted = no_unknown and (time_lifted or creds_lifted)
        return {
            "lifted": lifted,
            "time_lifted": time_lifted,
            "lifts_at": lifts_at,
            "required_credentials": [normalize_credential(c) for c in creds],
            "satisfied_credentials": satisfied_creds,
            "missing_credentials": missing_creds,
            "unknown_conditions": unknown_keys,
            "note": ban.get("lift_description")
            or lift_raw.get("reason")
            or ban.get("when_lifted"),
        }

    def _offer_requirements(
        self, offer: dict[str, Any]
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        raw_requirements = (
            offer.get("required_credentials")
            or offer.get("requires")
            or offer.get("credentials")
            or []
        )
        requirements = list(raw_requirements) if isinstance(raw_requirements, list) else [raw_requirements]
        conditions = offer.get("conditions")
        fact_requirements: list[dict[str, Any]] = []
        if isinstance(conditions, list):
            requirements.extend(conditions)
        elif isinstance(conditions, dict):
            nested = conditions.get("credentials") or conditions.get("required_credentials") or []
            requirements.extend(nested if isinstance(nested, list) else [nested])
            for key, expected in conditions.items():
                if key in {"credentials", "required_credentials"}:
                    continue
                fact_requirements.append({"name": key, "expected": expected})
        elif conditions is not None:
            # A scalar condition is evidence that must be supplied, never an
            # unknown condition that the engine silently assumes to be true.
            requirements.append(conditions)
        return requirements, fact_requirements

    def _evaluate_offer(
        self,
        offer_key: tuple[Any, ...],
        offer: dict[str, Any],
        req: dict[str, Any],
        matching: list[tuple[tuple[Any, ...], dict[str, Any]]],
        at: int,
    ) -> dict[str, Any]:
        requirements, fact_requirements = self._offer_requirements(offer)
        provided = req.get("credentials", [])
        missing: list[dict[str, Any]] = []
        satisfied: list[dict[str, Any]] = []
        used_waivers: list[dict[str, Any]] = []
        waiver_conflicts: list[dict[str, Any]] = []

        # Build valid current exceptions from all clauses; matching has not
        # necessarily marked exception current/status in this nested helper.
        for exc_key, exc in self.clauses.items():
            if self.clause_meta[exc_key]["received_at"] > at:
                continue
            if exc.get("kind") != "exception":
                continue
            matches, _ = self._clause_matches_request(exc, req)
            if not matches:
                continue
            if exc_key != self.current_revision_key(family_key_from_key(exc_key), at):
                continue
            if self._active_stop(exc_key, at) or self._clause_expired(exc, at):
                continue
            ref = parse_related_reference(exc)
            if not self._exception_references_offer(ref, offer, offer_key):
                continue
            waives = exc.get("waive") or exc.get("waives") or exc.get("covers_credentials") or []
            if not isinstance(waives, list):
                waives = [waives]
            for waived in waives:
                waived_norm = normalize_credential(waived)
                # Waivers are considered per requirement below.
                for requirement in requirements:
                    req_norm = normalize_credential(requirement)
                    if req_norm["id"] != waived_norm["id"]:
                        continue
                    if requirement_allows_waiver(requirement):
                        used_waivers.append(
                            {
                                "credential": req_norm,
                                "exception": clause_ref(exc),
                            }
                        )
                    else:
                        conflict = {
                            "type": "exception_beyond_jurisdiction",
                            "credential": req_norm,
                            "offer": clause_ref(offer),
                            "exception": clause_ref(exc),
                            "message": "商业例外不能覆盖非商业或明确不可豁免的条件",
                        }
                        if conflict not in waiver_conflicts:
                            waiver_conflicts.append(conflict)

        for requirement in requirements:
            req_norm = normalize_credential(requirement)
            match = next(
                (p for p in provided if credential_matches(requirement, p)), None
            )
            if match is not None:
                satisfied.append({"requirement": req_norm, "credential": match})
                continue
            waiver = next(
                (w for w in used_waivers if w["credential"]["id"] == req_norm["id"]),
                None,
            )
            if waiver:
                satisfied.append(
                    {"requirement": req_norm, "waived_by": waiver["exception"]}
                )
            else:
                missing.append(req_norm)

        missing_conditions: list[dict[str, Any]] = []
        facts = req.get("facts", {})
        raw = req.get("raw", {})
        for condition in fact_requirements:
            name = condition["name"]
            expected = condition["expected"]
            actual = facts.get(name, raw.get(name))
            if isinstance(expected, dict) and any(
                op in expected for op in (">=", "<=", ">", "<", "in")
            ):
                missing_conditions.append(
                    {
                        "name": name,
                        "expected": expected,
                        "actual": actual,
                        "reason": "unsupported_condition_operator",
                    }
                )
                continue
            if actual is None or actual != expected:
                missing_conditions.append(
                    {"name": name, "expected": expected, "actual": actual}
                )

        # Deduplicate used waivers by credential + exception identity.
        dedup_waivers: list[dict[str, Any]] = []
        for waiver in used_waivers:
            signature = (
                waiver["credential"]["id"],
                tuple(sorted(waiver["exception"].items())),
            )
            if not any(
                (w["credential"]["id"], tuple(sorted(w["exception"].items())))
                == signature
                for w in dedup_waivers
            ):
                dedup_waivers.append(waiver)

        return {
            "clause": clause_ref(offer),
            "satisfied": not missing and not missing_conditions,
            "required_credentials": [normalize_credential(r) for r in requirements],
            "satisfied_credentials": satisfied,
            "missing_credentials": missing,
            "missing_conditions": missing_conditions,
            "used_waivers": dedup_waivers,
            "waiver_conflicts": waiver_conflicts,
        }

    def _build_proof(self, offer_summary: dict[str, Any]) -> dict[str, Any]:
        offer_ref = offer_summary["clause"]
        offer_key = ref_to_key(offer_ref)
        offer = self.clauses[offer_key]
        exception_refs = [
            waiver["exception"] for waiver in offer_summary.get("used_waivers", [])
        ]
        clause_refs = [offer_ref, *exception_refs]
        until_values = [offer.get("until")]
        for ref in exception_refs:
            clause = self.clauses.get(ref_to_key(ref))
            if clause is not None:
                until_values.append(clause.get("until"))
        finite = [value for value in until_values if value is not None]
        return {
            "offer": offer_ref,
            "exceptions": exception_refs,
            "clause_refs": clause_refs,
            "clause_until": offer.get("until"),
            "proof_until": min(finite) if finite else None,
        }

    def _exception_references_offer(
        self,
        ref: dict[str, Any],
        offer: dict[str, Any],
        offer_key: tuple[Any, ...],
    ) -> bool:
        if not ref.get("clause") or str(ref["clause"]) != str(offer_key[3]):
            return False
        if ref.get("issuer") is not None and str(ref["issuer"]) != str(offer_key[0]):
            return False
        if ref.get("rev") is not None and not _revision_equal(ref["rev"], offer_key[4]):
            return False
        return offer.get("kind") == "offer"

    def _exception_summary(
        self, key: tuple[Any, ...], exception: dict[str, Any], at: int
    ) -> dict[str, Any]:
        ref = parse_related_reference(exception)
        target_keys = self._find_clause_keys(
            str(ref.get("clause")),
            ref.get("rev"),
            site=exception.get("site"),
            category=exception.get("category"),
            issuer=ref.get("issuer"),
        ) if ref.get("clause") else []
        valid_target = any(
            k in self.clauses
            and self.clauses[k].get("kind") == "offer"
            and self._active_stop(k, at) is None
            and not self._clause_expired(self.clauses[k], at)
            and (ref.get("rev") is not None or k in {
                self.current_revision_key(family_key_from_key(k), at)
            })
            for k in target_keys
        )
        return {
            "clause": clause_ref(exception),
            "related": ref,
            "references_valid_offer": valid_target,
            "waive": exception.get("waive") or [],
        }

    def _reapply_conditions(
        self,
        decision: str,
        missing_credentials: list[dict[str, Any]],
        missing_conditions: list[dict[str, Any]],
        bans: list[dict[str, Any]],
        completeness_state: dict[str, Any] | None,
    ) -> list[str]:
        conditions: list[str] = []
        if completeness_state is None or completeness_state.get("complete") is not True:
            conditions.append("由该门店上报 site/category 的 complete=true 三线核齐状态")
        for cred in missing_credentials:
            conditions.append(f"补齐并核验凭证 {cred['id']}，或取得引用有效允许条款的商业例外")
        for condition in missing_conditions:
            conditions.append(f"证明条件 {condition['name']}={condition.get('expected')!r}")
        for ban in bans:
            lift = ban.get("lift") or {}
            for cred in lift.get("missing_credentials", []):
                conditions.append(f"满足监管解禁凭证 {cred['id']}")
            if lift.get("lifts_at") is not None:
                conditions.append(f"等待监管解禁秒点 {lift['lifts_at']} 之后重新评估")
            if not lift.get("lifted") and not lift.get("missing_credentials") and lift.get("lifts_at") is None:
                conditions.append("取得监管机构新的有效放行/解禁条款后重新申请")
        if decision == "no_basis":
            conditions.append("取得当前管辖设备方对该型号与电池组合的有效 offer")
        return list(dict.fromkeys(conditions))

    # ------------------------------------------------------------------
    # Commitments
    # ------------------------------------------------------------------
    def _handle_commit(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        missing = [field for field in ("store", "request", "promise", "expires") if field not in record]
        if missing:
            return {
                "ok": False,
                "status": "invalid_commit",
                "errors": [f"缺少字段：{', '.join(missing)}"],
            }
        store = str(record["store"])
        request_id = str(record["request"])
        promise_id = str(record["promise"])
        if request_id not in self.requests:
            return {
                "ok": False,
                "status": "unknown_request",
                "promise": promise_id,
                "errors": [f"request {request_id} 尚未提交"],
            }
        request_entry = self.requests[request_id]
        if request_entry["store"] != store:
            return {
                "ok": False,
                "status": "store_mismatch",
                "request": request_id,
                "errors": ["只有提交 request 的同一门店可以将其签发为 promise"],
            }
        if promise_id in self.promises:
            existing = self.promises[promise_id]
            same = (
                existing["request_id"] == request_id
                and existing["store"] == store
                and existing["requested_expires"] == record["expires"]
            )
            if same:
                return {
                    "status": "promise_duplicate_ignored",
                    "promise": promise_id,
                    "promise_view": self.inspect_promise(promise_id, at),
                    "idempotent": True,
                }
            return {
                "ok": False,
                "status": "promise_id_conflict",
                "promise": promise_id,
                "errors": ["promise id 已对应不同承诺"],
            }

        expires = record["expires"]
        if type(expires) is not int or expires < 0:
            return {
                "ok": False,
                "status": "invalid_commit",
                "errors": ["expires 必须是非负整数秒点"],
            }
        if expires < at:
            return {
                "ok": False,
                "status": "commit_window_in_past",
                "promise": promise_id,
                "errors": ["期望到期秒点早于签发收到时刻"],
            }

        evaluation = self.evaluate_request(request_entry["normalized"], at=at)
        if not evaluation["can_commit"]:
            return {
                "ok": False,
                "status": "promise_not_issued",
                "decision": evaluation["decision"],
                "verdict": "reject" if evaluation["decision"] == "deny" else "hold",
                "approved": False,
                "can_commit": False,
                "promise": promise_id,
                "request": request_id,
                "evaluation": evaluation,
                "reason": evaluation["status"],
            }

        proofs = evaluation.get("proofs") or []

        def proof_window(proof: dict[str, Any]) -> int:
            if proof.get("proof_until") is None:
                return expires
            return min(expires, proof["proof_until"])

        primary_proof = max(proofs, key=proof_window) if proofs else None
        basis_refs = list((primary_proof or {}).get("clause_refs", []))
        constraints: list[dict[str, Any]] = [
            {"type": "requested_expires", "until": expires}
        ]
        valid_until = expires
        if primary_proof:
            valid_until = proof_window(primary_proof)
            offer_ref = primary_proof["offer"]
            offer = self.clauses[ref_to_key(offer_ref)]
            if offer.get("until") is not None:
                constraints.append(
                    {
                        "type": "clause_until",
                        "clause": clause_ref(offer),
                        "until": offer["until"],
                    }
                )
            for exception_ref in primary_proof.get("exceptions", []):
                exc = self.clauses.get(ref_to_key(exception_ref))
                if exc and exc.get("until") is not None:
                    constraints.append(
                        {
                            "type": "temporary_exception_until",
                            "clause": clause_ref(exc),
                            "until": exc["until"],
                        }
                    )

        # Deduplicate basis refs.
        dedup_basis: list[dict[str, Any]] = []
        seen = set()
        for ref in basis_refs:
            key = ref_to_key(ref)
            if key not in seen:
                seen.add(key)
                dedup_basis.append(ref)

        promise = {
            "id": promise_id,
            "store": store,
            "request_id": request_id,
            "site": request_entry["normalized"]["site"],
            "category": request_entry["normalized"]["category"],
            "purpose": request_entry["normalized"]["purpose"],
            "battery": request_entry["normalized"]["battery"],
            "request_snapshot": request_entry["normalized"],
            "issued_at": at,
            "issued_line": line,
            "requested_expires": expires,
            "valid_until": valid_until,
            "window_boundary": "inclusive",
            "constraints": constraints,
            "basis_refs": dedup_basis,
            "basis": evaluation["basis"],
            "primary_proof": primary_proof,
            "alternative_proofs": [p for p in proofs if p is not primary_proof],
            "completeness": evaluation.get("completeness"),
            "status": "issued",
            "redemption": None,
            "redemption_attempts": [],
            "stops": [],
            "post_delivery_stops": [],
            "review_required": False,
            "created_at": at,
            "timeline": [{"at": at, "event": "issued", "valid_until": valid_until}],
        }
        self.promises[promise_id] = promise
        request_entry.setdefault("promise_ids", []).append(promise_id)
        return {
            "status": "promise_issued",
            "decision": "allow",
            "verdict": "allow",
            "approved": True,
            "can_commit": True,
            "promise": promise_id,
            "valid_until": valid_until,
            "window": {"start": at, "end": valid_until, "boundary": "inclusive"},
            "basis": dedup_basis,
            "promise_view": self.inspect_promise(promise_id, at),
        }

    # ------------------------------------------------------------------
    # Redemption
    # ------------------------------------------------------------------
    def _handle_redeem(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        promise_id = record.get("promise")
        store = str(record.get("store")) if record.get("store") is not None else None
        if not promise_id or store is None:
            return {
                "ok": False,
                "status": "invalid_redeem",
                "errors": ["redeem 必须包含 store 和 promise"],
            }
        promise_id = str(promise_id)
        if promise_id not in self.promises:
            return {
                "ok": False,
                "status": "unknown_promise",
                "promise": promise_id,
                "decision": "unavailable",
                "verdict": "unavailable",
                "approved": False,
                "can_redeem": False,
                "consumed": False,
                "errors": ["中央服务找不到该承诺"],
            }
        promise = self.promises[promise_id]

        if promise.get("redemption"):
            prior = promise["redemption"]
            return {
                "ok": False,
                "status": "already_redeemed",
                "decision": "already_redeemed",
                "verdict": "duplicate",
                "approved": False,
                "can_redeem": False,
                "delivered": False,
                "promise": promise_id,
                "consumed": False,
                "duplicate": True,
                "prior_redemption": prior,
                "review_required": promise.get("review_required", False),
                "message": "同一承诺已被其他合成门店兑现，不能再次交付",
            }

        redeem_site = record.get("site", promise.get("site"))
        if redeem_site != promise.get("site"):
            result = {
                "ok": False,
                "status": "wrong_site",
                "decision": "deny",
                "verdict": "reject",
                "approved": False,
                "can_redeem": False,
                "promise": promise_id,
                "consumed": False,
                "required_site": promise.get("site"),
                "received_site": redeem_site,
                "message": "只有同一登记 site 的门店具备兑现资格",
            }
            promise.setdefault("redemption_attempts", []).append(
                {"at": at, "line": line, "store": store, "site": redeem_site, "result": "wrong_site"}
            )
            return result

        complete, completeness_state = self._completeness_ok(
            store, promise["site"], promise["category"], at
        )
        blockers = self._promise_blockers(promise, at)
        completeness_blocker = None
        if not complete:
            completeness_blocker = {
                "type": "completeness",
                "store": store,
                "site": promise["site"],
                "category": promise["category"],
                "reported": bool(completeness_state),
                "complete": False,
                "cursors": (completeness_state or {}).get("cursors"),
                "message": "兑现门店尚不能证明管辖范围内资料齐备，先挂起且不消费承诺",
            }

        if completeness_blocker:
            promise.setdefault("redemption_attempts", []).append(
                {
                    "at": at,
                    "line": line,
                    "store": store,
                    "site": redeem_site,
                    "result": "held_incomplete",
                    "known_blockers": blockers,
                }
            )
            return {
                "ok": False,
                "status": "held",
                "decision": "hold",
                "verdict": "hold",
                "approved": None,
                "can_redeem": False,
                "may_retry_after_completion": True,
                "promise": promise_id,
                "store": store,
                "consumed": False,
                "completeness_ok": False,
                "completeness": self._public_completeness(completeness_state),
                "blockers": [completeness_blocker, *blockers],
                "known_definitive_blockers": blockers,
                "message": "区域资料未齐：可查看已知依据，但本次不消费承诺；资料齐备后可重试",
            }

        if blockers:
            primary = blockers[0]
            status = primary.get("type", "blocked")
            if status == "stop":
                promise["status"] = "stopped"
                result_status = "stopped"
                decision = "deny"
            elif status == "window_expired":
                promise["status"] = "expired"
                result_status = "expired"
                decision = "deny"
            elif status == "ban":
                lift = primary.get("lift") or {}
                if lift.get("lifted"):
                    # Should not occur because active ban builder excludes lifted.
                    result_status = "allowed"
                    decision = "allow"
                elif lift.get("time_lifted") is False and lift.get("lifts_at") is not None:
                    result_status = "suspended"
                    decision = "hold"
                    promise["status"] = "suspended"
                else:
                    result_status = "banned"
                    decision = "deny"
                    promise["status"] = "banned"
            else:
                result_status = "blocked"
                decision = "deny"
            promise.setdefault("redemption_attempts", []).append(
                {
                    "at": at,
                    "line": line,
                    "store": store,
                    "site": redeem_site,
                    "result": result_status,
                    "blockers": blockers,
                }
            )
            return {
                "ok": False,
                "status": result_status,
                "decision": decision,
                "verdict": "hold" if decision == "hold" else "reject",
                "approved": False,
                "can_redeem": False,
                "promise": promise_id,
                "store": store,
                "consumed": False,
                "blockers": blockers,
                "reapply_conditions": self._promise_reapply(blockers),
            }

        confirmation = {
            "promise": promise_id,
            "store": store,
            "site": redeem_site,
            "redeemed_at": at,
            "line": line,
            "confirmation_code": f"SYNTH-RCPT-{promise_id}-{at}-{line}",
            "synthetic": True,
        }
        promise["redemption"] = confirmation
        promise["status"] = "redeemed"
        promise.setdefault("timeline", []).append(
            {"at": at, "event": "redeemed", "redemption": confirmation}
        )
        return {
            "status": "redeemed",
            "decision": "redeem",
            "verdict": "allow",
            "approved": True,
            "can_redeem": True,
            "delivered": True,
            "promise": promise_id,
            "store": store,
            "consumed": True,
            "redemption": confirmation,
            "basis": promise["basis_refs"],
        }

    def _promise_blockers(
        self, promise: dict[str, Any], at: int
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for ref in promise.get("basis_refs", []):
            key = ref_to_key(ref)
            stop = self._active_stop(key, at)
            if stop:
                blockers.append(
                    {
                        "type": "stop",
                        "clause": ref,
                        "stop": self._public_stop(stop),
                        "reason": stop.get("reason"),
                    }
                )
        if at > promise["valid_until"]:
            blockers.append(
                {
                    "type": "window_expired",
                    "valid_until": promise["valid_until"],
                    "at": at,
                    "message": "承诺有效窗口已过；临时条款到期会缩短该窗口",
                }
            )

        # A current hard ban blocks physical delivery.  Commercial exceptions
        # can never remove it.
        snapshot = promise.get("request_snapshot")
        if snapshot:
            for key, clause in self.clauses.items():
                if self.clause_meta[key]["received_at"] > at:
                    continue
                if clause.get("kind") != "ban":
                    continue
                matches, _ = self._clause_matches_request(clause, snapshot)
                if not matches:
                    continue
                if key not in self.current_revision_keymap(at):
                    continue
                if self._active_stop(key, at) or self._clause_expired(clause, at):
                    continue
                lift = self._ban_lift(clause, snapshot, at)
                if not lift.get("lifted"):
                    blockers.append(
                        {
                            "type": "ban",
                            "clause": clause_ref(clause),
                            "reason": clause.get("reason") or "有效监管禁止交付",
                            "lift": lift,
                        }
                    )
        return blockers

    def current_revision_keymap(self, at: int) -> set[tuple[Any, ...]]:
        keys = set()
        for family in self.family_index:
            current = self.current_revision_key(family, at)
            if current is not None:
                keys.add(current)
        return keys

    def _promise_reapply(self, blockers: list[dict[str, Any]]) -> list[str]:
        conditions: list[str] = []
        for blocker in blockers:
            if blocker["type"] == "window_expired":
                conditions.append("按当前有效条款重新提交 request 并取得新的 promise 窗口")
            elif blocker["type"] == "stop":
                conditions.append("等待停止兑现通知被有权机构解除，或按新 clause/rev 重新申请")
            elif blocker["type"] == "ban":
                lift = blocker.get("lift") or {}
                for cred in lift.get("missing_credentials", []):
                    conditions.append(f"补齐监管解禁凭证 {cred['id']} 后重新申请")
                if lift.get("lifts_at") is not None:
                    conditions.append(f"等待解禁秒点 {lift['lifts_at']}")
                if not lift.get("missing_credentials") and lift.get("lifts_at") is None:
                    conditions.append("取得监管机构新的放行条款后重新申请")
        return list(dict.fromkeys(conditions))

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------
    def _handle_inspect(
        self, record: dict[str, Any], at: int, line: int
    ) -> dict[str, Any]:
        request_ref = record.get("request")
        generic_ref = record.get("ref") or record.get("target")
        if not record.get("promise") and not request_ref and generic_ref:
            generic_ref = str(generic_ref)
            if generic_ref in self.promises:
                record = {**record, "promise": generic_ref}
            else:
                record = {**record, "request": generic_ref}
        if record.get("promise"):
            view = self.inspect_promise(str(record["promise"]), at)
            if view.get("ok") is False:
                return {"ok": False, "status": "unknown_promise", **view}
            return {"status": "inspect_promise", "view": view}
        if record.get("request") or record.get("id"):
            request_id = str(record.get("request") or record.get("id"))
            view = self.inspect_request(request_id, at)
            if view.get("ok") is False:
                return {"ok": False, "status": "unknown_request", **view}
            return {"status": "inspect_request", "view": view}
        return {
            "ok": False,
            "status": "invalid_inspect",
            "errors": ["inspect 需要 promise 或 request"],
        }

    def inspect_promise(self, promise_id: str, at: int | None = None) -> dict[str, Any]:
        at = self._at(at)
        promise = self.promises.get(str(promise_id))
        if promise is None:
            return {"ok": False, "error": f"unknown promise {promise_id}"}

        used_clauses = []
        for ref in promise["basis_refs"]:
            key = ref_to_key(ref)
            clause = self.clauses.get(key)
            if clause is None:
                used_clauses.append({"ref": ref, "available": False})
                continue
            stop = self._active_stop(key, at)
            current_key = self.current_revision_key(family_key_from_key(key), at)
            used_clauses.append(
                {
                    "ref": clause_ref(clause),
                    "available": True,
                    "record": clause,
                    "meta": self.clause_meta.get(key),
                    "expired_now": self._clause_expired(clause, at),
                    "active_stop_now": self._public_stop(stop) if stop else None,
                    "current_revision": current_key[4] if current_key else None,
                    "current_revision_ref": clause_ref(self.clauses[current_key])
                    if current_key is not None
                    else None,
                }
            )

        current_evaluation = self.evaluate_request(
            promise["request_snapshot"], at=at
        )
        blockers_now = self._promise_blockers(promise, at)
        completeness_reports = [
            self._public_completeness(state)
            for state in self.completeness_history
            if state.get("site") == promise["site"]
            and state.get("category") == promise["category"]
            and state["received_at"] <= at
        ]
        breaking_changes = self._breaking_changes(promise, at)
        return {
            "ok": True,
            "type": "promise",
            "promise": promise_id,
            "status": promise["status"],
            "store": promise["store"],
            "site": promise["site"],
            "category": promise["category"],
            "issued_at": promise["issued_at"],
            "requested_expires": promise["requested_expires"],
            "valid_until": promise["valid_until"],
            "window_boundary": promise.get("window_boundary", "inclusive"),
            "used_clauses": used_clauses,
            "basis_refs": promise["basis_refs"],
            "issuance_basis": promise.get("basis"),
            "completeness_at_issuance": promise.get("completeness"),
            "completeness_reports": completeness_reports,
            "redemption": promise.get("redemption"),
            "redeemed": promise.get("redemption") is not None,
            "review_required": promise.get("review_required", False),
            "review": promise.get("review"),
            "post_delivery_stops": promise.get("post_delivery_stops", []),
            "stops": promise.get("stops", []),
            "blockers_now": blockers_now,
            "current_rules_for_original_request": current_evaluation,
            "current_missing": current_evaluation.get("missing"),
            "breaking_changes": breaking_changes,
            "reapply_conditions": self._promise_reapply(blockers_now)
            or current_evaluation.get("reapply_conditions"),
            "redemption_attempts": promise.get("redemption_attempts", []),
            "timeline": promise.get("timeline", []),
        }

    def _breaking_changes(
        self, promise: dict[str, Any], at: int
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        redeemed_at = (promise.get("redemption") or {}).get("redeemed_at")

        for stop in promise.get("stops", []):
            effective_at = stop.get("effective_at")
            after_delivery = redeemed_at is not None and effective_at is not None and effective_at > redeemed_at
            changes.append(
                {
                    "change_type": stop.get("kind", "stop_notice"),
                    "clause": {
                        "issuer": stop.get("target_issuer") or stop.get("issuer"),
                        "site": stop.get("site"),
                        "category": stop.get("category"),
                        "clause": stop.get("clause"),
                        "rev": stop.get("rev"),
                    },
                    "effective_at": effective_at,
                    "reason": stop.get("reason"),
                    "breaking": True,
                    "effect": "review_after_delivery_preserve_fact"
                    if after_delivery
                    else "undelivered_promise_cannot_be_redeemed",
                }
            )

        if at > promise["valid_until"]:
            changes.append(
                {
                    "change_type": "temporary_or_window_expiry",
                    "breaking": True,
                    "effective_after": promise["valid_until"],
                    "effect": "undelivered_promise_expired_reapply_needed",
                }
            )

        # General revisions after issuance are non-breaking unless they carry
        # an explicit stop instruction.
        for ref in promise["basis_refs"]:
            key = ref_to_key(ref)
            family = family_key_from_key(key)
            for version_key in self.family_index.get(family, []):
                meta = self.clause_meta[version_key]
                if meta["received_at"] <= promise["issued_at"]:
                    continue
                clause = self.clauses[version_key]
                explicit_stop = self._stop_is_explicit_in_revision(clause)
                changes.append(
                    {
                        "change_type": "explicit_stop_in_revision"
                        if explicit_stop
                        else "general_revision",
                        "clause": clause_ref(clause),
                        "effective_at": meta["received_at"],
                        "breaking": explicit_stop,
                        "effect": "revision_explicitly_stops_existing_commitments"
                        if explicit_stop
                        else "promise_window_and_pinned_versions_remain_honored",
                    }
                )

        for blocker in self._promise_blockers(promise, at):
            if blocker["type"] == "ban":
                changes.append(
                    {
                        "change_type": "hard_ban",
                        "clause": blocker["clause"],
                        "breaking": True,
                        "effective_at": at,
                        "effect": "current_regulatory_ban_blocks_delivery",
                        "lift": blocker.get("lift"),
                    }
                )
        # Deduplicate by type/clause/effective.
        deduped: list[dict[str, Any]] = []
        seen = set()
        for change in changes:
            key = json.dumps(change, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                deduped.append(change)
        return deduped

    def inspect_request(self, request_id: str, at: int | None = None) -> dict[str, Any]:
        at = self._at(at)
        entry = self.requests.get(str(request_id))
        if entry is None:
            return {"ok": False, "error": f"unknown request {request_id}"}
        evaluation = self.evaluate_request(entry["normalized"], at=at)
        promise_views = [
            self.inspect_promise(pid, at)
            for pid in entry.get("promise_ids", [])
            if pid in self.promises
        ]
        return {
            "ok": True,
            "type": "request",
            "request_id": request_id,
            "store": entry["store"],
            "received_at": entry["received_at"],
            "record": entry["raw"],
            "evaluation": evaluation,
            "promise_ids": entry.get("promise_ids", []),
            "promises": promise_views,
        }

    # ------------------------------------------------------------------
    # High-level commissioner / store APIs
    # ------------------------------------------------------------------
    def publish_authority(
        self,
        issuer: str,
        role: str,
        site: str,
        category: str,
        revision: int,
        active: bool = True,
        at: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        record = {
            "issuer": issuer,
            "role": role,
            "site": site,
            "category": category,
            "revision": revision,
            "active": active,
            **extra,
        }
        return self.handle(self._envelope("authority", record, at))

    def end_authority(
        self,
        issuer: str,
        role: str,
        site: str,
        category: str,
        revision: int,
        at: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        return self.publish_authority(
            issuer, role, site, category, revision, False, at, **extra
        )

    def publish_clause(
        self, record: dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        return self.handle(self._envelope("clause", record, at))

    def stop_clause(
        self,
        issuer: str,
        clause: str,
        rev: Any,
        reason: str = "",
        at: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        record = {"issuer": issuer, "clause": clause, "rev": rev, "reason": reason, **extra}
        return self.handle(self._envelope("notify_stop", record, at))

    def report_completeness(
        self,
        store: str,
        site: str,
        category: str,
        complete: bool,
        cursors: dict[str, Any] | None = None,
        at: int | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "store": store,
            "site": site,
            "category": category,
            "complete": complete,
        }
        if cursors is not None:
            record["cursors"] = cursors
        return self.handle(self._envelope("completeness", record, at))

    def submit_request(
        self, record: dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        return self.handle(self._envelope("request", record, at))

    def _envelope(
        self, kind: str, record: dict[str, Any], at: int | None
    ) -> dict[str, Any]:
        return {"at": self.current_at if at is None else at, "kind": kind, "record": record}

    def query(
        self, record: dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        with self._lock:
            return self.evaluate_request(record, at)

    def issue(
        self, record: dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        envelope = {"at": self.current_at if at is None else at, "kind": "commit", "record": record}
        return self.handle(envelope)

    def redeem(
        self, record: dict[str, Any], at: int | None = None
    ) -> dict[str, Any]:
        envelope = {"at": self.current_at if at is None else at, "kind": "redeem", "record": record}
        return self.handle(envelope)

    def inspect(
        self, record: dict[str, Any] | str, at: int | None = None
    ) -> dict[str, Any]:
        if isinstance(record, str):
            if record in self.promises:
                return self.inspect_promise(record, at)
            return self.inspect_request(record, at)
        envelope = {"at": self.current_at if at is None else at, "kind": "inspect", "record": record}
        return self.handle(envelope)


# Public semantic aliases.
RuleEngine = RuleMiddleware
Middleware = RuleMiddleware
CentralRuleService = RuleMiddleware
RuleCenter = RuleMiddleware
RuleMiddleware.decide = RuleMiddleware.evaluate_request
RuleMiddleware.evaluate = RuleMiddleware.evaluate_request


class RegionalCommissioner:
    """区域专员的职责发布/结束门面。"""

    def __init__(self, center: RuleMiddleware | None = None) -> None:
        self.center = center or RuleMiddleware()

    def publish_responsibility(self, **kwargs: Any) -> dict[str, Any]:
        return self.center.publish_authority(**kwargs)

    def end_responsibility(self, **kwargs: Any) -> dict[str, Any]:
        return self.center.end_authority(**kwargs)

    publish = publish_responsibility
    end = end_responsibility


if __name__ == "__main__":
    from main import main

    raise SystemExit(main())

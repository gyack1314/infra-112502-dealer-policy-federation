#!/usr/bin/env python3
"""回放 JSONL 演练事件，并输出结构化回执。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Keep absolute-path and ``python -I /workspace/main.py`` invocations working.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rule_middleware import MESSAGE_KINDS, RuleMiddleware  # noqa: E402
from scenario_reader import load_events  # noqa: E402


def replay_file(path: Path, pretty: bool = False) -> list[dict]:
    middleware = RuleMiddleware()
    events = list(load_events(path, set(MESSAGE_KINDS)))
    results = middleware.process_events(events)
    for result in results:
        if pretty:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return results


def replay_stdin(pretty: bool = False) -> list[dict]:
    middleware = RuleMiddleware()
    results: list[dict] = []
    for line_no, line in enumerate(sys.stdin, 1):
        if not line.strip():
            continue
        event = json.loads(line)
        result = middleware.handle(event)
        results.append(result)
        if pretty:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return results


def summary_file(path: Path) -> dict:
    middleware = RuleMiddleware()
    results = middleware.process_events(load_events(path, set(MESSAGE_KINDS)))
    promises = {
        pid: {
            "status": p["status"],
            "valid_until": p["valid_until"],
            "redeemed": bool(p.get("redemption")),
            "review_required": p.get("review_required", False),
        }
        for pid, p in middleware.promises.items()
    }
    return {
        "file": str(path),
        "events": len(results),
        "rejected_clauses": len(middleware.rejected_clauses),
        "invalid_notices": len(middleware.invalid_notices),
        "promises": promises,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行管辖感知规则中间件演练")
    parser.add_argument("paths", nargs="*", type=Path, help="JSONL 事件文件")
    parser.add_argument("--pretty", action="store_true", help="缩进输出完整回执")
    parser.add_argument("--summary", action="store_true", help="只输出每个文件的摘要")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取 JSONL")
    parser.add_argument("--serve", metavar="HOST:PORT", help="启动本机 HTTP 中央服务，例如 127.0.0.1:8765")
    args = parser.parse_args(argv)

    if args.serve:
        from http_service import serve

        host, port_text = args.serve.rsplit(":", 1)
        serve(host or "127.0.0.1", int(port_text))
        return 0

    if not args.paths and not args.stdin:
        parser.error("提供 JSONL 文件、--stdin，或使用 --serve HOST:PORT")

    summaries = []
    paths: list[Path] = []
    for path in args.paths:
        if str(path) == "-":
            continue
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        else:
            paths.append(path)
    try:
        if args.stdin or any(str(path) == "-" for path in args.paths):
            if args.summary:
                parser.error("--summary 请配合文件路径使用")
            replay_stdin(pretty=args.pretty)
        for path in paths:
            if args.summary:
                summaries.append(summary_file(path))
            else:
                replay_file(path, pretty=args.pretty)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "status": "input_error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    if args.summary:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

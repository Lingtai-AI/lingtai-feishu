#!/usr/bin/env python3
"""Read-only footprint audit for the lingtai-feishu MCP addon.

Reports what this addon has left under an agent working directory:
  - accounts (subdirs of <agent_dir>/feishu/)
  - inbox + sent message.json files
  - attachment files (any file under any inbox/<uuid>/attachments/ dir)
  - voice/audio candidate files (by extension)
  - total bytes for attachments

This script never deletes or mutates addon data. The only write it
performs is appending a single JSONL record to
<agent_dir>/logs/cleanup.jsonl describing the audit it just ran.

Run:
    python3 scripts/footprint_audit.py <agent_dir>
    python3 scripts/footprint_audit.py <agent_dir> --json
    python3 scripts/footprint_audit.py <agent_dir> --max-paths 50
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

TOOL_NAME = "lingtai-feishu"
ADDON_DIR = "feishu"

VOICE_EXTS = {
    ".ogg", ".opus", ".mp3", ".wav", ".m4a", ".oga", ".webm", ".aac", ".flac",
}


def _iter_files(root: Path):
    if not root.exists() or not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            yield Path(dirpath) / fname


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def audit(agent_dir: Path, max_paths: int) -> dict:
    root = agent_dir / ADDON_DIR
    accounts: list[str] = []
    inbox_msgs = 0
    sent_msgs = 0
    attachment_files: list[tuple[Path, int]] = []
    voice_files: list[Path] = []

    if root.exists() and root.is_dir():
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            accounts.append(entry.name)

            inbox_dir = entry / "inbox"
            if inbox_dir.is_dir():
                for msg_dir in inbox_dir.iterdir():
                    if not msg_dir.is_dir():
                        continue
                    if (msg_dir / "message.json").is_file():
                        inbox_msgs += 1
                    att_dir = msg_dir / "attachments"
                    if att_dir.is_dir():
                        for f in _iter_files(att_dir):
                            size = _safe_size(f)
                            attachment_files.append((f, size))
                            if f.suffix.lower() in VOICE_EXTS:
                                voice_files.append(f)

            sent_dir = entry / "sent"
            if sent_dir.is_dir():
                for msg_dir in sent_dir.iterdir():
                    if not msg_dir.is_dir():
                        continue
                    if (msg_dir / "message.json").is_file():
                        sent_msgs += 1

    total_bytes = sum(sz for _, sz in attachment_files)
    candidates = inbox_msgs + sent_msgs + len(attachment_files)

    attachment_files.sort(key=lambda t: t[1], reverse=True)
    top_paths = [
        {"path": str(p), "bytes": sz}
        for p, sz in attachment_files[:max_paths]
    ]

    return {
        "tool": TOOL_NAME,
        "root": str(root),
        "root_exists": root.exists(),
        "accounts": accounts,
        "account_count": len(accounts),
        "inbox_messages": inbox_msgs,
        "sent_messages": sent_msgs,
        "attachment_files": len(attachment_files),
        "voice_candidates": len(voice_files),
        "attachment_bytes": total_bytes,
        "candidates": candidates,
        "top_attachments": top_paths,
    }


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} {u}"
        f /= 1024
    return f"{n} B"


def _print_report(report: dict) -> None:
    print(f"== {report['tool']} footprint audit ==")
    print(f"root: {report['root']} (exists={report['root_exists']})")
    print(f"accounts: {report['account_count']} {report['accounts']}")
    print(f"inbox messages:  {report['inbox_messages']}")
    print(f"sent messages:   {report['sent_messages']}")
    print(
        f"attachment files: {report['attachment_files']} "
        f"({_human_bytes(report['attachment_bytes'])})"
    )
    print(f"voice/audio candidates: {report['voice_candidates']}")
    print(f"total candidates: {report['candidates']}")
    if report["top_attachments"]:
        print("top attachments (largest first):")
        for item in report["top_attachments"]:
            print(f"  {_human_bytes(item['bytes']):>10}  {item['path']}")
    print()
    print(
        "NOTE: this is a read-only report. No files were deleted. "
        "Cleanup requires an explicit dry-run + user consent step."
    )


def _append_audit_record(agent_dir: Path, report: dict) -> Path:
    logs_dir = agent_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "cleanup.jsonl"
    record = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool": report["tool"],
        "dry_run": True,
        "human_approved": False,
        "candidates": report["candidates"],
        "bytes": report["attachment_bytes"],
        "root": report["root"],
        "summary": (
            f"accounts={report['account_count']} "
            f"inbox={report['inbox_messages']} "
            f"sent={report['sent_messages']} "
            f"attachments={report['attachment_files']} "
            f"voice={report['voice_candidates']} "
            f"bytes={report['attachment_bytes']}"
        ),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return log_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only footprint audit for lingtai-feishu.",
    )
    parser.add_argument("agent_dir", help="Path to the agent working directory")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON only (no human-readable report)",
    )
    parser.add_argument(
        "--max-paths", type=int, default=20,
        help="Max number of largest attachment paths to include (default: 20)",
    )
    args = parser.parse_args(argv)

    agent_dir = Path(args.agent_dir).expanduser().resolve()
    report = audit(agent_dir, max_paths=max(0, args.max_paths))

    try:
        log_path = _append_audit_record(agent_dir, report)
        report["audit_log"] = str(log_path)
    except OSError as exc:
        report["audit_log_error"] = str(exc)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
        if "audit_log" in report:
            print(f"audit record appended: {report['audit_log']}")
        if "audit_log_error" in report:
            print(f"audit log write failed: {report['audit_log_error']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

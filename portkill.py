#!/usr/bin/env python3
"""portkill - see what is listening on your ports and free them in one command."""

import argparse
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time

__version__ = "1.0.0"
IS_WINDOWS = platform.system() == "Windows"


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _split_addr(local):
    host, _, port = local.rpartition(":")
    return host.strip("[]") or "*", port


def _linux():
    rows = []
    for line in _run(["ss", "-ltnpH"]).splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        host, port = _split_addr(parts[3])
        if not port.isdigit():
            continue
        m = re.search(r'\("([^"]*)",pid=(\d+)', line)
        rows.append({
            "port": int(port),
            "address": host,
            "pid": int(m.group(2)) if m else None,
            "name": m.group(1) if m else "?",
        })
    return rows


def _macos():
    rows, pid, name = [], None, "?"
    for line in _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"]).splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p" and val.isdigit():
            pid = int(val)
        elif tag == "c":
            name = val
        elif tag == "n":
            host, port = _split_addr(val)
            if port.isdigit():
                rows.append({"port": int(port), "address": host, "pid": pid, "name": name})
    return rows


def _windows():
    names = {}
    for line in _run(["tasklist", "/FO", "CSV", "/NH"]).splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[1].isdigit():
            names[int(cells[1])] = cells[0]
    rows = []
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[4].isdigit():
            host, port = _split_addr(parts[1])
            if port.isdigit():
                pid = int(parts[4])
                rows.append({"port": int(port), "address": host, "pid": pid,
                             "name": names.get(pid, "?")})
    return rows


def listeners():
    """Return one row per (port, pid) that is listening on TCP."""
    system = platform.system()
    reader = _windows if system == "Windows" else _macos if system == "Darwin" else _linux
    seen, rows = set(), []
    for row in sorted(reader(), key=lambda r: (r["port"], r["pid"] or 0)):
        key = (row["port"], row["pid"])
        if key not in seen:
            seen.add(key)
            rows.append(row)
    return rows


def _alive(pid):
    if IS_WINDOWS:
        return f" {pid} " in " " + " ".join(_run(["tasklist", "/FI", f"PID eq {pid}", "/NH"]).split()) + " "
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill(pid, force=False):
    """Stop a process. Returns True once it is gone."""
    if IS_WINDOWS:
        _run(["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else []))
        time.sleep(0.5)
        return not _alive(pid)
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(30):  # wait up to 3 seconds for a graceful exit
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _table(rows):
    if not rows:
        return "Nothing is listening."
    out = [f"{'PORT':<7}{'PID':<9}{'ADDRESS':<18}PROCESS"]
    for r in rows:
        out.append(f"{r['port']:<7}{str(r['pid'] or '-'):<9}{r['address']:<18}{r['name']}")
    return "\n".join(out)


def build_parser():
    p = argparse.ArgumentParser(
        prog="portkill",
        description="List what is listening on TCP ports and free the ones you name.",
        epilog="Examples:  portkill   |   portkill 3000   |   portkill 3000 8080 -y   |   portkill 5173 --force",
    )
    p.add_argument("ports", nargs="*", type=int, help="port(s) to free; omit to just list listeners")
    p.add_argument("-f", "--force", action="store_true", help="use SIGKILL / taskkill /F instead of a graceful stop")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be stopped, change nothing")
    p.add_argument("--json", action="store_true", help="print the listener list as JSON")
    p.add_argument("-V", "--version", action="version", version=f"portkill {__version__}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rows = listeners()

    if not args.ports:
        print(json.dumps(rows, indent=2) if args.json else _table(rows))
        return 0

    wanted = set(args.ports)
    targets = [r for r in rows if r["port"] in wanted]
    for port in sorted(wanted - {r["port"] for r in targets}):
        print(f"Port {port}: nothing is listening.", file=sys.stderr)
    if not targets:
        return 1

    print(_table(targets))
    if args.dry_run:
        print("\nDry run: nothing was stopped.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to stop processes without a terminal; pass -y to confirm.", file=sys.stderr)
            return 2
        if input("\nStop these processes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    failed = 0
    for r in targets:
        pid = r["pid"]
        if pid is None:
            print(f"! port {r['port']}: owner unknown (try again with sudo / as administrator)")
            failed += 1
        elif pid == os.getpid():
            continue
        elif kill(pid, args.force):
            print(f"✔ freed port {r['port']} ({r['name']}, pid {pid})")
        else:
            hint = "" if args.force else " - try --force"
            print(f"✘ could not stop {r['name']} (pid {pid}) on port {r['port']}{hint}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

import argparse
import json
import os
import socket
import sys
from datetime import datetime

SOCKET_PATH = os.environ.get("MIRRORD_SOCKET", "/tmp/mirrord/control.sock")


def _send(action: str, plugin: str = "", socket_path: str | None = None) -> dict:
    path = socket_path or SOCKET_PATH
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5.0)
        sock.connect(path)
        msg = json.dumps({"action": action, "plugin": plugin}) + "\n"
        sock.sendall(msg.encode())
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        return json.loads(data.decode().strip())
    except FileNotFoundError:
        print(f"Error: control socket not found at {path}", file=sys.stderr)
        print("Is the mirrord server running?", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: connection refused at {path}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        print(f"Error: server closed connection at {path}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print("Error: connection timed out", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("Error: invalid response from server", file=sys.stderr)
        sys.exit(1)
    finally:
        sock.close()


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_trigger(args: argparse.Namespace) -> None:
    key = args.plugin or ""
    resp = _send("trigger", key, socket_path=args.socket)
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    print("Triggered all plugins" if not key else f"Triggered: {key}")


def cmd_stop(args: argparse.Namespace) -> None:
    key = args.plugin or ""
    resp = _send("stop", key, socket_path=args.socket)
    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    print("Stopped all plugins" if not key else f"Stopped: {key}")


def cmd_status(args: argparse.Namespace) -> None:
    resp = _send("status", socket_path=args.socket)
    plugins = resp.get("plugins", [])
    if not plugins:
        print("No plugins configured.")
        return
    if args.json:
        print(json.dumps(resp, indent=2))
        return
    width_name = max(len(p["name"]) for p in plugins)
    width_slug = max(len(p["slug"]) for p in plugins)
    for p in plugins:
        name = p["name"].ljust(width_name)
        slug = p["slug"].ljust(width_slug)
        status = p["status"].upper().ljust(8)
        last = _fmt_ts(p["last_sync"])
        dur = f"{p['last_duration']:.1f}s" if p["last_duration"] is not None else "-"
        print(f"  {name}  [{slug}]  {status}  last: {last}  dur: {dur}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mirrord",
        description="Control the mirrord mirror sync server",
    )
    parser.add_argument(
        "--socket",
        default=SOCKET_PATH,
        help=f"Control socket path (default: {SOCKET_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    trigger_p = sub.add_parser("trigger", help="Trigger a mirror sync immediately")
    trigger_p.add_argument(
        "plugin", nargs="?", default="", help="Plugin slug or name (omit for all)"
    )
    trigger_p.set_defaults(func=cmd_trigger)

    stop_p = sub.add_parser("stop", help="Stop a running mirror sync")
    stop_p.add_argument(
        "plugin", nargs="?", default="", help="Plugin slug or name (omit for all)"
    )
    stop_p.set_defaults(func=cmd_stop)

    status_p = sub.add_parser("status", help="Show sync status for all plugins")
    status_p.add_argument("--json", action="store_true", help="Output as JSON")
    status_p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

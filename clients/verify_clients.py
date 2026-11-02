r"""End-to-end smoke test against an isolated local server database.

Run from the repository root after building the compiled clients:
    .\.venv\Scripts\python.exe clients\verify_clients.py
"""

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
import psutil
import socket as socket_module


ROOT = Path(__file__).resolve().parent.parent
CLIENTS = {
    "powershell": [os.environ.get("BOT_TEST_POWERSHELL", "pwsh"), "-NoProfile", "-File", str(ROOT / "clients/powershell/client.ps1")],
    "go": [str(ROOT / "clients/go/build/botyara-go.exe")],
    "rust": [str(ROOT / "clients/rust/target/release/botyara-rust.exe")],
    "cpp": [str(ROOT / "clients/cpp/build/botyara-cpp.exe")],
    "dotnet": ([os.environ["BOT_TEST_DOTNET_EXE"]] if os.environ.get("BOT_TEST_DOTNET_EXE") else
               [str(ROOT / "clients/dotnet/bin/Release/net40/BotyaraClient.exe")]),
}


def lines_from(process, output):
    for line in process.stdout:
        output.put(line.rstrip())


def until(predicate, timeout, label):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value is not None:
            return value
        time.sleep(0.1)
    raise TimeoutError(label)


def run_one(name, profile, base, directory, cwd=None):
    config = directory / f"{name}-{profile}.json"
    if profile == "custom" and not config.exists():
        rules = {
            "checkin": {"method": "POST", "transport": "form", "field": "ticket"},
            "get_task": {"method": "GET", "transport": "query", "field": "callback"},
            "post_task": {"method": "POST", "transport": "json", "field": "answer"},
        }
        response = requests.post(base + "/payloads", json={"profile": rules}, timeout=5)
        response.raise_for_status()
        config.write_text(json.dumps({**response.json(), "server_url": base}), encoding="utf-8")
    env = os.environ.copy()
    env.update(BOT_SERVER=base, BOT_PROFILE="standard" if profile == "custom" else profile,
               BOT_CONFIG=str(config), BOT_SLEEP="0.1", BOT_JITTER="0")
    process = subprocess.Popen(CLIENTS[name], cwd=cwd or ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
    output = queue.Queue()
    reader = threading.Thread(target=lines_from, args=(process, output), daemon=True)
    reader.start()
    log = []
    try:
        def callback_line():
            while not output.empty():
                line = output.get_nowait(); log.append(line)
                if line.startswith("Callback:"):
                    return line.split(":", 1)[1].strip()
            if process.poll() is not None:
                raise RuntimeError("client exited before callback: " + " | ".join(log))
            return None
        callback = until(callback_line, 15, f"{name}/{profile} callback")
        for command in ("whoami", "ip", "exit"):
            response = requests.post(base + "/tasks", json={"callback_uuid": callback, "command": command}, timeout=5)
            response.raise_for_status()
            task_id = response.json()["task_id"]
            def completed():
                state = requests.get(base + "/state", timeout=5).json()
                for task in state["tasks"]:
                    if task["id"] == task_id and task["status"] == "completed":
                        return task["result"]
                return None
            result = until(completed, 20, f"{name}/{profile}/{command} result")
            if not result or result.startswith("Ошибка:"):
                raise AssertionError(f"{name}/{profile}/{command}: {result!r}")
            if command == "whoami":
                expected = subprocess.check_output(["whoami"], text=True, encoding="oem").strip()
                if result.casefold() != expected.casefold():
                    raise AssertionError(f"{name}/{profile}: whoami mismatch: {result!r} != {expected!r}")
            if command == "ip":
                expected = {address.address.split("%", 1)[0]
                            for addresses in psutil.net_if_addrs().values() for address in addresses
                            if address.family in (socket_module.AF_INET, socket_module.AF_INET6)}
                actual = {line.rsplit(": ", 1)[-1].split("%", 1)[0] for line in result.splitlines()}
                if actual != expected:
                    raise AssertionError(f"{name}/{profile}: IP mismatch: {actual ^ expected}")
            if command == "exit" and result != "Выхожу":
                raise AssertionError(f"{name}/{profile}: exit mismatch: {result!r}")
            print(f"PASS {name:10s} {profile:8s} {command:6s} {len(result)} chars")
            if os.environ.get("BOT_TEST_VERBOSE") and command == "ip":
                print(ascii(result))
        process.wait(timeout=5)
        reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError("client output reader did not finish")
        if process.returncode != 0:
            raise RuntimeError(f"client exited {process.returncode}: " + " | ".join(log))
        while not output.empty(): log.append(output.get_nowait())
        return log
    except Exception:
        while not output.empty(): log.append(output.get_nowait())
        print(f"LOG {name}/{profile}: " + " | ".join(log[-20:]), file=sys.stderr)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=CLIENTS, action="append")
    parser.add_argument("--profile", choices=("standard", "mixed", "host", "custom"), action="append")
    parser.add_argument("--reuse", action="store_true", help="start each client twice with the same config")
    args = parser.parse_args()
    names = args.client or list(CLIENTS)
    profiles = args.profile or ["standard", "mixed", "host", "custom"]
    with tempfile.TemporaryDirectory(prefix="botyara-clients-") as temp:
        directory = Path(temp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy(); env.update(BOT_PORT=str(port), BOT_DATABASE=str(directory / "server.db"))
        server = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        try:
            def ready():
                try: return True if requests.get(base + "/ping", timeout=0.5).ok else None
                except requests.RequestException: return None
            until(ready, 10, "server startup")
            for name in names:
                for profile in profiles:
                    run_one(name, profile, base, directory)
                    if args.reuse: run_one(name, profile, base, directory)
        finally:
            server.terminate()
            try: server.wait(timeout=3)
            except subprocess.TimeoutExpired: server.kill()


if __name__ == "__main__":
    main()

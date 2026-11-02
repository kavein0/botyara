"""Run with the repository's .venv Python; all servers/configs/DBs are temporary."""
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "clients"))
import verify_clients as verify
import requests
import psutil


def serve():
    sys.path.insert(0, str(ROOT))
    import server
    from flask import request
    server.init_db()
    enabled = [False]
    before, after = set(), set()

    @server.app.post("/test-enable-faults")
    def enable():
        enabled[0] = True
        return {"ok": True}

    @server.app.before_request
    def fail_before_save():
        if enabled[0] and request.path == "/results":
            data = (request.get_json(silent=True) or {}).get("data", {})
            key = data.get("task_id")
            if key is not None and key not in before:
                before.add(key)
                return {"error": "test: before save"}, 503

    @server.app.after_request
    def fail_after_save(response):
        if enabled[0] and request.path == "/results" and response.status_code == 200:
            data = (request.get_json(silent=True) or {}).get("data", {})
            key = data.get("task_id")
            if key is not None and key not in after:
                after.add(key)
                response = server.app.make_response(({"error": "test: after save"}, 503))
        return response

    server.app.run(host="127.0.0.1", port=int(os.environ["BOT_PORT"]))


def launch_case(exe, base, directory, profile=None, config=None, expected=None):
    env = os.environ.copy()
    for key in ("BOT_PROFILE", "BOT_CONFIG"):
        env.pop(key, None)
    env.update(BOT_SERVER=base, BOT_SLEEP="0.1", BOT_JITTER="0")
    if profile is not None: env["BOT_PROFILE"] = profile
    if config is not None: env["BOT_CONFIG"] = str(config)
    process = subprocess.Popen([str(exe)], cwd=directory, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    output, log = queue.Queue(), []
    reader = threading.Thread(target=verify.lines_from, args=(process, output), daemon=True)
    reader.start()
    try:
        def callback_line():
            while not output.empty():
                line = output.get_nowait(); log.append(line)
                if line.startswith("Callback:"): return line.split(":", 1)[1].strip()
            if process.poll() is not None: raise AssertionError(log)
            return None
        callback = verify.until(callback_line, 15, "callback")
        effective = next(line.split(": ", 1)[1].split()[0] for line in log if line.startswith("Effective profile:"))
        assert effective in ("standard", "mixed", "host", "custom"), log
        if expected: assert effective == expected, log
        if profile is None and config is None:
            assert log[0] == "Profile selection: random; requested: " + effective, log
            assert (directory / ("payload-" + effective + ".json")).exists()
            assert len(list(directory.glob("*.json"))) == 1
        # A compatible CLR runs inside this PID; no companion client process or mapped client file.
        maps = [m.path for m in psutil.Process(process.pid).memory_maps()]
        assert any("framework64" in m.lower() and m.lower().endswith("\\clr.dll") for m in maps), maps
        assert not any("botyaraclient" in m.lower() for m in maps), maps
        assert not psutil.Process(process.pid).children(), "Unexpected child process"
        task = requests.post(base + "/tasks", json={"callback_uuid": callback, "command": "exit"}, timeout=5)
        task.raise_for_status()
        task_id = task.json()["task_id"]
        def completed():
            for item in requests.get(base + "/state", timeout=5).json()["tasks"]:
                if item["id"] == task_id and item["status"] == "completed": return item["result"]
        assert verify.until(completed, 15, "exit result") == "Выхожу"
        assert process.wait(timeout=5) == 0
        reader.join(timeout=5)
        assert not reader.is_alive(), "Client output reader did not finish"
        while not output.empty(): log.append(output.get_nowait())
        assert sum(line.startswith("Profile selection:") for line in log) == 1
        return effective
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)


def main():
    source = ROOT / "clients/rustclr-loader/dist/Loader.exe"
    with tempfile.TemporaryDirectory(prefix="botyara-loader-") as temporary:
        directory = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        base = "http://127.0.0.1:" + str(port)
        env = os.environ.copy()
        env.update(BOT_PORT=str(port), BOT_DATABASE=str(directory / "server.db"))
        server = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--server"], cwd=directory, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def ready():
            try: return True if requests.get(base + "/ping", timeout=.5).ok else None
            except requests.RequestException: return None
        try:
            verify.until(ready, 15, "server")
            clean = directory / "clean"
            clean.mkdir()
            exe = clean / "Loader.exe"
            shutil.copy2(source, exe)
            assert list(clean.iterdir()) == [exe]
            verify.CLIENTS["dotnet"] = [str(exe)]
            configs = directory / "configs"; configs.mkdir()
            for profile in ("standard", "mixed", "host", "custom"):
                first = verify.run_one("dotnet", profile, base, configs, cwd=clean)
                path = configs / ("dotnet-" + profile + ".json")
                saved = path.read_bytes()
                second = verify.run_one("dotnet", profile, base, configs, cwd=clean)
                assert path.read_bytes() == saved
                assert any("Effective profile: " + profile in line for line in first + second)
            print("PASS isolated EXE: four profiles, all commands, config bytes preserved")
            seen = set()
            for i in range(9):
                case = directory / ("random-" + str(i)); case.mkdir()
                copied = case / "Loader.exe"; shutil.copy2(source, copied)
                seen.add(launch_case(copied, base, case))
            print("PASS random launches: only built-ins, one choice per launch; observed", sorted(seen))
            case = directory / "priority"; case.mkdir()
            launch_case(exe, base, case, profile="host", expected="host")
            launch_case(exe, base, case, config=case / "missing.json", expected="standard")
            launch_case(exe, base, case, profile="mixed", config=case / "explicit.json", expected="mixed")
            custom = configs / "dotnet-custom.json"
            saved = custom.read_bytes()
            launch_case(exe, base, case, profile="host", config=custom, expected="custom")
            launch_case(exe, base, case, config=custom, expected="custom")
            assert custom.read_bytes() == saved
            print("PASS BOT_PROFILE/BOT_CONFIG priorities and saved custom rules")
            for label, options, fragment in [
                ("server mismatch", {"BOT_CONFIG": str(custom), "BOT_SERVER": base + "/wrong"}, "does not match"),
                ("invalid profile", {"BOT_PROFILE": "custom"}, "Invalid BOT_PROFILE"),
            ]:
                e = env.copy()
                e.pop("BOT_CONFIG", None); e.pop("BOT_PROFILE", None)
                e["BOT_SERVER"] = base
                e.update(options)
                result = subprocess.run([str(exe)], cwd=case, env=e, capture_output=True, text=True, encoding="utf-8", timeout=15)
                assert result.returncode == 1 and fragment in result.stderr, (label, result)
            invalid = case / "invalid.json"
            data = json.loads(custom.read_text(encoding="utf-8")); data["profile"]["checkin"]["transport"] = "bad"
            invalid.write_text(json.dumps(data), encoding="utf-8")
            e = env.copy(); e.update(BOT_SERVER=base, BOT_CONFIG=str(invalid), BOT_PROFILE="standard")
            result = subprocess.run([str(exe)], cwd=case, env=e, capture_output=True, text=True, encoding="utf-8", timeout=15)
            assert result.returncode == 1 and "Invalid profile rule" in result.stderr
            print("PASS nonzero errors: server mismatch, invalid profile, invalid saved rules")
            requests.post(base + "/test-enable-faults", timeout=5).raise_for_status()
            for name, binary in [("net40", ROOT / "clients/dotnet/bin/Release/net40/BotyaraClient.exe"), ("loader", exe)]:
                verify.CLIENTS["dotnet"] = [str(binary)]
                fault_dir = directory / ("fault-" + name); fault_dir.mkdir()
                log = verify.run_one("dotnet", "standard", base, fault_dir, cwd=clean)
                assert sum(line.startswith("Task:") for line in log) == 3, log
                assert sum(line.startswith("Result sent:") for line in log) == 3, log
                assert sum("Connection failed, retrying:" in line for line in log) == 6, log
                print("PASS", name, "503 before/after save for each command: 9 sends, 3 executions, exit acknowledged")
            assert list(clean.iterdir()) == [exe], list(clean.iterdir())
            print("PASS clean run directory still contains only Loader.exe")
        finally:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)


if __name__ == "__main__":
    serve() if "--server" in sys.argv else main()

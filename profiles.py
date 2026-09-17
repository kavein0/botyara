import base64
import re
import secrets
import time


STANDARD_PROFILE = {
    "checkin": {"method": "POST", "transport": "json", "field": "payload_uuid"},
    "get_task": {"method": "POST", "transport": "json", "field": "callback_uuid"},
    "post_task": {"method": "POST", "transport": "json", "field": "data"},
}

MIXED_PROFILE = {
    "checkin": {"method": "GET", "transport": "query", "field": "id"},
    "get_task": {"method": "POST", "transport": "form", "field": "cb"},
    "post_task": {"method": "POST", "transport": "form", "field": "data"},
}

HOST_PROFILE = {
    "checkin": {"method": "POST", "transport": "host", "field": "payload_uuid"},
    "get_task": {"method": "POST", "transport": "host", "field": "callback_uuid"},
    "post_task": {"method": "POST", "transport": "host", "field": "data"},
}

PROFILES = {"standard": STANDARD_PROFILE, "mixed": MIXED_PROFILE, "host": HOST_PROFILE}

HOST_SUFFIX = "demo.test"
HOST_PART_SIZE = 50
HOST_MAX_BYTES = 20000
HOST_MESSAGES = {}

def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("Профиль должен быть объектом")
    for operation in ("checkin", "get_task", "post_task"):
        rule = profile.get(operation)
        if not isinstance(rule, dict):
            raise ValueError(f"Нужно правило {operation}")
        if rule.get("method") not in ("GET", "POST"):
            raise ValueError("Метод должен быть GET или POST")
        if rule.get("transport") not in ("json", "form", "query", "host"):
            raise ValueError("Transport должен быть json, form, query или host")
        field = rule.get("field")
        if not isinstance(field, str) or not field.strip() or len(field) > 64:
            raise ValueError("Имя поля должно содержать от 1 до 64 символов")
        if rule["method"] == "GET" and rule["transport"] not in ("query", "host"):
            raise ValueError("Для GET используй query или host")

def encode_host_data(text):
    data = text.encode("utf-8")
    if not data or len(data) > HOST_MAX_BYTES:
        raise ValueError(f"Нужно от 1 до {HOST_MAX_BYTES} байт UTF-8")
    return base64.b32encode(data).decode("ascii").rstrip("=")

def decode_host_data(encoded):
    padding = "=" * (-len(encoded) % 8)
    data = base64.b32decode(encoded.upper() + padding)
    if len(data) > HOST_MAX_BYTES:
        raise ValueError("Сообщение слишком длинное")
    return data.decode("utf-8")

def split_host_parts(text):
    encoded = encode_host_data(text)
    return [encoded[i:i + HOST_PART_SIZE] for i in range(0, len(encoded), HOST_PART_SIZE)]

def parse_host_header(host):
    match = re.fullmatch(
        r"m\.([0-9a-f]{12})\.([0-9]{1,3})\.([0-9]{1,3})\.([a-z2-7]{1,50})\." + re.escape(HOST_SUFFIX),
        host or "", re.IGNORECASE,
    )
    if not match:
        return None
    message_id, number, total, part = match.groups()
    return message_id.lower(), int(number), int(total), part.upper()

def assemble_host_message(message_id, number, total, part):
    max_parts = ((HOST_MAX_BYTES * 8 + 4) // 5 + HOST_PART_SIZE - 1) // HOST_PART_SIZE
    if not 1 <= number <= total <= max_parts:
        raise ValueError("Неверный номер части")
    now = time.monotonic()
    for key, value in list(HOST_MESSAGES.items()):
        if now - value["updated"] > 60:
            del HOST_MESSAGES[key]
    if message_id not in HOST_MESSAGES and len(HOST_MESSAGES) >= 100:
        raise ValueError("Слишком много незавершённых сообщений")
    state = HOST_MESSAGES.setdefault(message_id, {
        "total": total, "parts": {}, "updated": now,
    })
    old = state["parts"].get(number)
    if state["total"] != total or (old is not None and old != part):
        raise ValueError("Части противоречат друг другу")
    state["parts"][number] = part
    state["updated"] = now
    if len(state["parts"]) != total:
        return None
    encoded = "".join(state["parts"][i] for i in range(1, total + 1))
    del HOST_MESSAGES[message_id]
    return decode_host_data(encoded)
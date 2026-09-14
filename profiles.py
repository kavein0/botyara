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

PROFILES = {"standard": STANDARD_PROFILE, "mixed": MIXED_PROFILE}


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("Профиль должен быть объектом")
    for operation in ("checkin", "get_task", "post_task"):
        rule = profile.get(operation)
        if not isinstance(rule, dict):
            raise ValueError(f"Нужно правило {operation}")
        if rule.get("method") not in ("GET", "POST"):
            raise ValueError("Метод должен быть GET или POST")
        if rule.get("transport") not in ("json", "form", "query"):
            raise ValueError("Transport должен быть json, form или query")
        field = rule.get("field")
        if not isinstance(field, str) or not field.strip() or len(field) > 64:
            raise ValueError("Имя поля должно содержать от 1 до 64 символов")
        if rule["method"] == "GET" and rule["transport"] != "query":
            raise ValueError("Для GET используй query")

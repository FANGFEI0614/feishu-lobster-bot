from flask import Flask, request, jsonify
import requests
import subprocess
import os
import json
import time

app = Flask(__name__)

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
OPENCLAW_AGENT_ID = os.environ.get("OPENCLAW_AGENT_ID", "main").strip()

_token_cache = {
    "token": None,
    "expire_at": 0,
}

# 简单内存去重：message_id -> timestamp
processed_messages = {}
DEDUP_TTL_SECONDS = 600  # 10分钟


def cleanup_processed_messages():
    now = time.time()
    expired = [
        mid for mid, ts in processed_messages.items()
        if now - ts > DEDUP_TTL_SECONDS
    ]
    for mid in expired:
        processed_messages.pop(mid, None)


def already_processed(message_id: str) -> bool:
    cleanup_processed_messages()
    return message_id in processed_messages


def mark_processed(message_id: str):
    processed_messages[message_id] = time.time()


def get_tenant_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"] - 60:
        return _token_cache["token"]

    if not APP_ID or not APP_SECRET:
        raise RuntimeError("FEISHU_APP_ID or FEISHU_APP_SECRET is not set")

    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": APP_ID,
            "app_secret": APP_SECRET,
        },
        timeout=20,
    )

    print("get_tenant_access_token status:", resp.status_code)
    print("get_tenant_access_token body:", resp.text)

    data = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError(f"get tenant_access_token http error: {data}")

    if data.get("code") != 0:
        raise RuntimeError(f"get tenant_access_token failed: {data}")

    token = data["tenant_access_token"]
    expire = int(data.get("expire", 7200))

    _token_cache["token"] = token
    _token_cache["expire_at"] = now + expire
    return token


def send_feishu_message(receive_id: str, text: str, receive_id_type: str = "open_id"):
    token = get_tenant_access_token()

    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)

    print("send_feishu_message status:", resp.status_code)
    print("send_feishu_message body:", resp.text)

    data = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError(f"send message http error: {data}")

    if data.get("code") != 0:
        raise RuntimeError(f"send message failed: {data}")

    return data


def reply_in_same_chat(event: dict, text: str):
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {})
    open_id = (sender_id.get("open_id") or "").strip()

    if not open_id:
        raise RuntimeError("open_id not found in event payload")

    return send_feishu_message(open_id, text, receive_id_type="open_id")


def run_openclaw_task(user_text: str) -> str:
    try:
        result = subprocess.run(
            [
                "openclaw",
                "agent",
                "--agent",
                OPENCLAW_AGENT_ID,
                "--message",
                user_text,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            err = stderr or stdout or "Unknown OpenClaw error"
            return f"OpenClaw 执行失败：{err[:1500]}"

        output = stdout or stderr or "OpenClaw returned no output."
        return output[:1800]

    except subprocess.TimeoutExpired:
        return "OpenClaw task timed out."
    except Exception as e:
        return f"OpenClaw task failed: {e}"


def extract_user_text(data: dict) -> str:
    event = data.get("event", {})
    message = event.get("message", {})
    text_content = message.get("content", "")

    if not text_content:
        return ""

    try:
        parsed = json.loads(text_content)
        return (parsed.get("text") or "").strip()
    except Exception:
        return str(text_content).strip()


@app.route("/feishu", methods=["POST"])
def feishu_webhook():
    data = request.json or {}
    print("Incoming Feishu payload:", json.dumps(data, ensure_ascii=False))

    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    try:
        event = data.get("event", {})
        sender = event.get("sender", {})
        sender_type = sender.get("sender_type", "")
        message = event.get("message", {})
        message_type = message.get("message_type", "")
        message_id = (message.get("message_id") or "").strip()

        if message_type and message_type != "text":
            return jsonify({"ok": True})

        if sender_type == "app":
            return jsonify({"ok": True})

        if not message_id:
            return jsonify({"ok": True})

        if already_processed(message_id):
            print(f"Duplicate message ignored: {message_id}")
            return jsonify({"ok": True})

        user_text = extract_user_text(data)
        if not user_text:
            return jsonify({"ok": True})

        mark_processed(message_id)
        print(f"User text: {user_text}")
        print(f"Processing message_id: {message_id}")

        answer = run_openclaw_task(user_text)
        reply_in_same_chat(event, f"{answer}")

        return jsonify({"ok": True})

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
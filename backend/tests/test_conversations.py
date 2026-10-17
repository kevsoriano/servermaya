from datetime import UTC

import requests
from fastapi import HTTPException

import main
from core import auth, config, db
from helpers import client
from integrations import messenger
from routes import conversations
from services import chats


def test_conversations(monkeypatch):
    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": "u1"}
    monkeypatch.setattr(config, "PAGE_ID", "99")
    monkeypatch.setattr(config, "PAGE_TOKEN", "tok")
    calls = []

    class Resp:
        ok = True

        def json(self):
            return {
                "data": [
                    {"id": "m2", "message": "how much?", "from": {"id": "7", "name": "Ana"}},
                    {"id": "m1", "message": "hello", "from": {"id": "99", "name": "Shop"}},
                ]
            }

    monkeypatch.setattr(requests, "request", lambda method, url, **k: calls.append((url, k)) or Resp())
    try:
        msgs = client.get("/api/conversations/t_123").json()
        assert [m["id"] for m in msgs] == ["m1", "m2"]  # oldest first
        assert [m["from_page"] for m in msgs] == [True, False]
        assert "tok" not in calls[0][0]  # token is not in the URL
        assert client.get("/api/conversations/me%3Ffields=access_token").status_code == 400
        assert len(calls) == 1
    finally:
        main.app.dependency_overrides.clear()


def test_attachments_are_normalised():
    msg = {
        "sticker": "https://cdn/sticker.png",
        "attachments": {
            "data": [
                {"mime_type": "image/jpeg", "name": "a.jpg", "image_data": {"url": "https://cdn/a.jpg", "preview_url": "https://cdn/a_p.jpg"}},
                {"mime_type": "video/mp4", "name": "v.mp4", "video_data": {"url": "https://cdn/v.mp4", "preview_url": "https://cdn/v.jpg"}},
                {"mime_type": "audio/mp4", "name": "voice.mp4", "file_url": "https://cdn/voice.mp4"},
                {"mime_type": "application/pdf", "name": "r.pdf", "file_url": "https://cdn/r.pdf"},
                {"mime_type": "image/png", "name": "bad", "image_data": {"url": "javascript:alert(1)"}},
                {"mime_type": "image/png", "name": "missing"},
            ]
        },
    }
    got = messenger.attachments(msg)
    assert [(a["kind"], a["url"]) for a in got] == [
        ("image", "https://cdn/a.jpg"),
        ("video", "https://cdn/v.mp4"),
        ("audio", "https://cdn/voice.mp4"),
        ("file", "https://cdn/r.pdf"),
        ("image", "https://cdn/sticker.png"),
    ]
    assert got[0]["preview_url"] == "https://cdn/a_p.jpg"
    assert messenger.attachments({"message": "text only"}) == []


def test_reply_window_and_send(monkeypatch):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+0000")
    msgs = [{"from_page": False, "created_time": iso(now - timedelta(hours=30))}, {"from_page": True, "created_time": iso(now - timedelta(hours=1))}]
    # Only the customer's messages open the window; the Page's own replies do not extend it.
    assert chats.reply_until(msgs) < now
    assert chats.reply_until([msgs[1]]) is None

    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": "u1", "email": "s@example.com"}
    monkeypatch.setattr(config, "PAGE_ID", "99")
    sent, stored = [], []
    monkeypatch.setattr(messenger, "fetch_messages", lambda cid: msgs)
    monkeypatch.setattr(
        messenger,
        "graph",
        lambda path, send=None, files=None, **k: (
            sent.append((path, send, files))
            or ({"participants": {"data": [{"id": "99"}, {"id": "psid7"}]}} if "fields" in k else {"message_id": "mid.1"})
        ),
    )

    class Store:
        def collection(self, *_):
            return self

        def document(self, *_):
            return self

        def add(self, doc):
            stored.append(doc)

    monkeypatch.setattr(db, "clients", lambda: (None, Store()))
    try:
        assert client.post("/api/conversations/t_1/reply", json={"text": "hi"}).status_code == 409  # window closed
        assert sent == []
        msgs[0]["created_time"] = iso(now - timedelta(hours=2))
        assert client.post("/api/conversations/t_1/reply", json={"text": ""}).status_code == 422
        r = client.post("/api/conversations/t_1/reply", json={"text": "Here is your link", "order_id": "o1"})
        assert r.status_code == 200 and r.json() == {"message_id": "mid.1"}
        assert sent[-1] == (
            "99/messages",
            {"recipient": {"id": "psid7"}, "messaging_type": "RESPONSE", "message": {"text": "Here is your link"}},
            None,
        )
        assert stored[0]["sent_by"] == "s@example.com" and stored[0]["order_id"] == "o1"

        # An image, such as the seller's GCash QR: uploaded to Messenger as a file and kept as evidence.
        uploads = []
        monkeypatch.setattr(
            db,
            "bucket",
            lambda: type(
                "B", (), {"blob": lambda self, p: type("Blob", (), {"upload_from_string": lambda self, data, **k: uploads.append((p, data))})()}
            )(),
        )
        image = lambda body, kind: client.post("/api/conversations/t_1/reply/image?order_id=o1", content=body, headers={"Content-Type": kind})
        assert image(b"x", "image/heic").status_code == 415  # Messenger would not show it
        assert image(b"", "image/png").status_code == 413
        r = image(b"png bytes", "image/png")
        assert r.status_code == 200 and r.json() == {"message_id": "mid.1"}
        path, send, files = sent[-1]
        assert send["message"] == {"attachment": {"type": "image", "payload": {"is_reusable": False}}} and files["filedata"][1:] == (
            b"png bytes",
            "image/png",
        )
        assert uploads[0][0].startswith("stores/main/sent/") and stored[-1]["image"]["size"] == 9 and stored[-1]["order_id"] == "o1"
    finally:
        main.app.dependency_overrides.clear()


def test_graph_uploads_send_objects_as_json_form_fields(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "PAGE_ID", "99")
    monkeypatch.setattr(config, "PAGE_TOKEN", "t")
    monkeypatch.setattr(requests, "request", lambda method, url, **k: seen.update(k) or type("R", (), {"ok": True, "json": lambda self: {}})())
    messenger.graph("99/messages", {"recipient": {"id": "7"}, "messaging_type": "RESPONSE"}, {"filedata": ("a.png", b"x", "image/png")})
    assert seen["data"] == {"recipient": '{"id": "7"}', "messaging_type": "RESPONSE"} and "json" not in seen


def test_thread_list_read_state_and_pending_actions(monkeypatch):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S+0000")
    thread = lambda cid, sender, at: {
        "id": cid,
        "updated_time": iso(at),
        "participants": {"data": [{"id": "99"}, {"id": "7", "name": "Ana"}]},
        "messages": {"data": [{"from": {"id": sender}, "created_time": iso(at)}]},
    }
    main.app.dependency_overrides[auth.current_user] = lambda: {"uid": "u1"}
    monkeypatch.setattr(config, "PAGE_ID", "99")
    monkeypatch.setattr(
        messenger,
        "graph",
        lambda path, **k: {
            "data": [
                thread("t_1", "7", now - timedelta(hours=21)),  # customer waiting, window closing
                thread("t_2", "99", now - timedelta(hours=1)),  # seller replied last
                thread("t_3", "7", now - timedelta(hours=2)),  # customer waiting, but seen since
            ]
        },
    )
    monkeypatch.setattr(conversations, "cached_open_checks", lambda cid: None)
    monkeypatch.setattr(conversations, "demo_list", lambda user: [])
    monkeypatch.setattr(conversations, "read_marks", lambda user: {"t_3": now - timedelta(hours=1)})
    monkeypatch.setattr(conversations, "order_actions", lambda ids: {"t_2": ["Waiting for payment"]})
    try:
        t1, t2, t3 = client.get("/api/conversations").json()
        assert (t1["unread"], t1["awaiting_reply"]) == (True, True)
        assert datetime.fromisoformat(t1["reply_until"]) - now < timedelta(hours=4)
        assert (t2["unread"], t2["awaiting_reply"], t2["reply_until"]) == (False, False, None)
        assert t2["order_actions"] == ["Waiting for payment"]
        assert (t3["unread"], t3["awaiting_reply"]) == (False, True)
        assert client.post("/api/conversations/bad/seen").status_code == 400
    finally:
        main.app.dependency_overrides.clear()

    assert conversations.order_action({"status": "draft"}) == "Send the draft order"
    assert conversations.order_action({"status": "confirmed"}) == "Request payment"
    paid = {"status": "confirmed", "version": 1, "payment": {"status": "verified", "version": 1}}
    assert conversations.order_action(paid) == "Ready to pack"
    assert conversations.order_action({**paid, "packing": {"version": 1, "completed": None}}) == "Ready to pack"  # photos, not done
    v2 = {**paid, "version": 2, "payment": {"status": "verified", "version": 2}}
    assert conversations.order_action({**v2, "packing": {"version": 1, "completed": {"by": "s"}}}) == "Ready to pack"  # packed for v1 only
    assert conversations.order_action({"status": "cancelled"}) is None


def test_profile_pics_batched_cached_and_soft_failing(monkeypatch):
    calls = []

    def fake(path, **k):
        calls.append(k["ids"])
        if k["ids"] == "bad":
            raise HTTPException(502, "nope")
        return {"a": {"profile_pic": "https://pic/a"}, "b": {}}

    monkeypatch.setattr(messenger, "graph", fake)
    monkeypatch.setattr(messenger, "_pics", {})
    assert messenger.profile_pics(["a", "b"]) == {"a": "https://pic/a", "b": None}
    assert messenger.profile_pics(["a", "b"]) == {"a": "https://pic/a", "b": None}
    assert calls == ["a,b"]  # second call served from cache
    assert messenger.profile_pics(["bad"]) == {"bad": None}
    messenger._pics["a"] = (0, "old")  # expired
    messenger.profile_pics(["a"])
    assert calls[-1] == "a" and messenger._pics["a"][1] == "https://pic/a"


def test_human_agent_tag_lets_a_person_reply_for_seven_days_only_when_switched_on(monkeypatch):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    monkeypatch.setattr(config, "PAGE_ID", "99")
    monkeypatch.setattr(messenger, "fetch_messages", lambda cid: [{"from_page": False, "created_time": (now - timedelta(hours=30)).isoformat()}])
    monkeypatch.setattr(messenger, "graph", lambda *a, **k: {"participants": {"data": [{"id": "99"}, {"id": "psid7"}]}})
    try:
        chats.customer_to_reply("t_1")
        raise AssertionError("30 hours after the customer's message, a plain reply is refused")
    except HTTPException as e:
        assert e.status_code == 409 and "24 hours" in e.detail
    monkeypatch.setattr(config, "HUMAN_AGENT", True)
    assert chats.customer_to_reply("t_1") == ("psid7", "HUMAN_AGENT")
    assert chats.messaging("HUMAN_AGENT") == {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}

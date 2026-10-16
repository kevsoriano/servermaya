from datetime import UTC

import pytest
from fastapi import HTTPException

from core import config
from routes import conversations
from routes import gcash as gcash_routes


def test_gcash_receipt_checks():
    from datetime import datetime

    from domain import receipts as gc

    sent = datetime(2026, 10, 15, 14, 0, tzinfo=gc.PHT)
    arrived = datetime(2026, 10, 15, 14, 20, tzinfo=gc.PHT)
    seller = {"name": "Juan Miguel Dela Cruz", "mobile": "0917 123 1234"}
    good = {
        "readable": True,
        "outcome": "successful",
        "amount_php": 1250.37,
        "reference": "1012 345 678901",
        "paid_at": "2026-10-15T14:12",
        "recipient_name": "JU** MI**** D.",
        "recipient_mobile": "+63 917 *** 1234",
    }

    def run(used=frozenset(), **k):
        return {c["check"]: c["result"] for c in gc.check_receipt(gc.Receipt(**{**good, **k}), 125037, sent, arrived, seller, set(used))}

    assert set(run().values()) == {"pass"}
    assert run(outcome="failed")["outcome"] == "fail"  # a "Your transfer has failed" screen still shows amount and recipient
    assert run(outcome="pending")["outcome"] == "fail"
    assert run(outcome="not_shown")["outcome"] == "unreadable"
    assert run(amount_php=1250.0)["amount"] == "fail"  # the centavos matter
    assert run(amount_php=None)["amount"] == "unreadable"

    assert run(paid_at="2026-10-14T09:30")["time"] == "fail"  # an old receipt
    assert run(paid_at="2026-10-15T14:40")["time"] == "fail"  # dated after the screenshot arrived
    assert run(paid_at="2026-10-15T13:59")["time"] == "pass"  # minute precision: within the slack
    assert run(paid_at="Oct 15, 2:12 PM")["time"] == "unreadable"

    assert run(used={"1012345678901"})["reference"] == "fail"  # reused on another order
    assert run(reference="12345")["reference"] == "fail"
    assert run(reference=None)["reference"] == "unreadable"
    assert run(reference="757427")["reference"] == "pass"  # a bank app's InstaPay trace, as on a BPI receipt
    assert gc.reference_key("757427", gc.Receipt(**good)) == "20261015-757427"  # traces repeat, so the index keys them by day
    assert gc.reference_key("757427", gc.Receipt(**{**good, "paid_at": None})) is None
    assert gc.reference_key("1012345678901", gc.Receipt(**good)) == "1012345678901"

    assert run(recipient_name="JU** DE** C.")["recipient_name"] == "pass"  # middle name not shown
    assert run(recipient_name="MA** SA****")["recipient_name"] == "fail"
    assert run(recipient_name="DE** JU**")["recipient_name"] == "fail"  # wrong order
    assert run(recipient_name="*** ***")["recipient_name"] == "fail"  # nothing to compare

    assert run(recipient_mobile="09171231234")["recipient_mobile"] == "pass"
    assert run(recipient_mobile="+63 917 *** 9999")["recipient_mobile"] == "fail"
    assert run(recipient_mobile="+63 918 *** 1234")["recipient_mobile"] == "fail"  # same last four, other number
    assert run(recipient_mobile="+63 917 *** **34")["recipient_mobile"] == "pass"
    assert run(recipient_mobile="+63 9** *** **34")["recipient_mobile"] == "unreadable"  # too little to compare

    # A BPI InstaPay receipt shows the end of the GCash account ID from the seller's QR instead of the number.
    qr_seller = {**seller, "user_id": "DWQM4TK3JDNWF205D"}
    bank = lambda account: {
        c["check"]: c["result"]
        for c in gc.check_receipt(
            gc.Receipt(**{**good, "recipient_mobile": None, "recipient_account": account}), 125037, sent, arrived, qr_seller, set()
        )
    }["recipient_mobile"]
    assert bank("xxxxxxxxxxxxxx05D") == "pass"
    assert gc.masked("KE**N G** S. (QR Code)") == "KE**N G** S."  # BPI's label after the name
    assert bank("xxxxxxxxxxxxxx06D") == "fail"
    assert bank("xxxxxxxxxxxxxxx5D") == "unreadable"  # too little to compare
    assert run(recipient_mobile=None, recipient_account="xxxxxxxxxxxxxx05D")["recipient_mobile"] == "unreadable"  # no QR saved

    # The format on the seller's real QR card: letters hidden mid-word, last digits hidden with bullets.
    kevin = {"name": "Kevin Gil Soriano", "mobile": "639272561234", "masked_name": "KE**N G** S."}
    receipt = gc.Receipt(**{**good, "recipient_name": "KE**N G** S.", "recipient_mobile": "+63  927 256 ••••"})
    by_check = {c["check"]: c for c in gc.check_receipt(receipt, 125037, sent, arrived, kevin, set())}
    assert by_check["recipient_name"]["detail"].endswith("exactly how GCash shows the seller's name")
    assert by_check["recipient_mobile"]["result"] == "pass"
    assert gc.name_fits("KE**N G** S.", "Kevin Soriano") is False  # the G word is missing
    assert gc.name_fits("KE**N S.", "Kevin Gil Soriano")  # a middle name may be left out
    assert gc.name_fits("KE**N G** S.", "Ken Gil Soriano") is False  # ** hides at least one letter
    assert gc.mobile_fits(gc.mobile("+63 927 256 ••••"), "639172561234") is False

    # Copies: the same file, or the same amount and minute under another reference.
    check = lambda **k: {c["check"]: c for c in gc.check_receipt(gc.Receipt(**good), 125037, sent, arrived, seller, set(), **k)}["duplicate"]
    assert check(image_reused=True)["result"] == "fail"
    assert check(twin_reference="9999999999999")["result"] == "fail"
    assert gc.details_key(gc.Receipt(**good)) == "125037_20261015T1412"
    assert gc.details_key(gc.Receipt(**{**good, "paid_at": None})) is None

    assert gc.check_receipt(gc.Receipt(**{**good, "readable": False}), 125037, sent, arrived, seller, set())[0]["result"] == "unreadable"


def test_gcash_manual_payment_rules():
    from datetime import datetime

    from domain import orders as rules
    from domain import receipts as gc

    now = datetime.now(UTC)
    assert gc.settings(gc.Settings(name="  Juan  Dela Cruz ", mobile="0917 123 1234")) == {
        "name": "Juan Dela Cruz",
        "mobile": "639171231234",
        "qr": None,
    }
    with pytest.raises(rules.Refused):
        gc.settings(gc.Settings(name="Juan", mobile="1234"))

    pay_to = {"name": "Juan Dela Cruz", "mobile": "639171231234"}
    order = {"status": "draft", "version": 1, "draft": {"total": 125037}}
    with pytest.raises(rules.Refused):
        gc.request(order, pay_to, "me", now)  # not confirmed
    order["status"] = "confirmed"
    with pytest.raises(rules.Refused):
        gc.current(order)  # nothing requested yet
    order |= gc.request(order, pay_to, "me", now)
    m = order["manual_payment"]
    assert (m["status"], m["amount"], m["pay_to"], m["requested_at"]) == ("requested", 125037, pay_to, now)
    assert gc.request(order, pay_to, "me", datetime.now(UTC)) == {}  # the first request time stands
    assert conversations.order_action(order) == "Waiting for GCash payment"

    passing = [gc.check("amount", "pass", ""), gc.check("time", "pass", "")]
    claim = lambda id, ref, checks=passing: {"id": id, "message_id": f"m_{id}", "reference": ref, "checks": checks}
    order |= gc.add_claims(order, [claim("a", "1012345678901"), claim("b", "2012345678901", [gc.check("time", "fail", "old")])])
    assert order["manual_payment"]["status"] == "claimed" and conversations.order_action(order) == "Review a GCash receipt"
    assert gc.add_claims(order, [claim("a", "1012345678901")]) == {}  # each message once

    with pytest.raises(rules.Refused):
        gc.confirm(order, "12345", "me", "", now, False)  # not a GCash reference
    with pytest.raises(rules.Refused):
        gc.confirm(order, "1012345678901", "me", "", now, True)  # already confirmed on another order
    with pytest.raises(rules.Refused, match="time"):
        gc.confirm(order, "2012345678901", "me", "", now, False)  # a failed check needs a reason
    with pytest.raises(rules.Refused, match="no screenshot"):
        gc.confirm(order, "3012345678901", "me", "", now, False)
    assert (
        gc.confirm(order, "3012345678901", "me", "Customer paid in person at pickup", now, False)["manual_payment"]["confirmed"]["claim_id"] is None
    )

    # A bank payment: GCash history shows its own reference, and the invno ends in the bank receipt's trace.
    shown = {
        "readable": True,
        "outcome": "successful",
        "amount_php": 83.0,
        "reference": "757427",
        "paid_at": "2026-10-15T23:59",
        "recipient_name": "KE**N G** S.",
        "recipient_mobile": None,
    }
    bank = {**order, "manual_payment": {**order["manual_payment"], "claims": [{**claim("t", "757427"), "receipt": shown}]}}
    matched = lambda invno: gc.confirm(bank, "1046160730748", "me", "", now, False, invno)["manual_payment"]["confirmed"]["claim_id"] == "t"
    assert matched("20261015BOPI PHMMXXXB00019511075 7427")
    assert matched("20261016BOPIPHMMXXXB000195110757427")  # dated by the bank just after midnight
    for other in ("20261015BOPIPHMMXXXB000195110757428", "20260914BOPIPHMMXXXB000195110757427"):  # other digits; an older transfer
        with pytest.raises(rules.Refused):
            gc.confirm(bank, "1046160730748", "me", "", now, False, other)

    # The seller's own GCash history screenshot fills in the reference; a failed check on it needs a reason too.
    entry = gc.HistoryEntry(
        readable=True,
        direction="received",
        amount_php=1250.37,
        reference="1012 345 678901",
        paid_at=f"{now.astimezone(gc.PHT):%Y-%m-%dT%H:%M}",
        invno=None,
    )
    assert {c["result"] for c in gc.check_history(entry, 125037, now)} == {"pass"}
    checks = lambda **k: {c["check"]: c["result"] for c in gc.check_history(entry.model_copy(update=k), 125037, now)}
    assert checks(direction="sent")["direction"] == "fail"
    assert checks(amount_php=1250.0)["amount"] == "fail"
    assert checks(paid_at="2026-01-01T09:00")["time"] == "fail"  # came in before payment was asked for
    assert checks(reference="757427")["reference"] == "fail"  # history always shows GCash's own 13 digits
    read = lambda **k: {"image": {"path": "p"}, "reference": "1012345678901", "checks": gc.check_history(entry.model_copy(update=k), 125037, now)}
    with pytest.raises(rules.Refused):
        gc.confirm(order | gc.add_history(order, read(direction="sent")), "1012345678901", "me", "", now, False)
    with_history = order | gc.add_history(order, read())
    assert gc.confirm(with_history, "1012345678901", "me", "", now, False)["manual_payment"]["confirmed"]["history_image"] == {"path": "p"}

    order |= gc.confirm(order, "1012 3456 78901", "me", "", now, False)
    c = order["manual_payment"]["confirmed"]
    assert order["manual_payment"]["status"] == "seller_confirmed" and (c["reference"], c["claim_id"], c["reason"]) == ("1012345678901", "a", None)
    assert conversations.order_action(order) is None
    assert gc.add_claims(order, [claim("c", "4012345678901")]) == {}  # nothing changes after confirming
    with pytest.raises(rules.Refused):
        rules.reusable_payment(order)  # no PayMongo request on top of a confirmed GCash payment
    with pytest.raises(rules.Refused):
        gc.request(order, pay_to, "me", now)

    # A new version needs a new request; the old one stays on record and no longer counts.
    order["version"] = 2
    with pytest.raises(rules.Refused):
        gc.current(order)
    assert rules.reusable_payment(order) is None
    order |= gc.request(order, pay_to, "me", now)
    assert order["manual_payment"]["version"] == 2 and order["manual_payment_history"][0]["status"] == "seller_confirmed"

    # A PayMongo-verified payment for this version wins.
    paid = {**order, "payment": {"status": "verified", "version": 2}}
    with pytest.raises(rules.Refused):
        gc.request({**paid, "manual_payment": None}, pay_to, "me", now)
    assert conversations.order_action(paid) is None


def test_gcash_payment_the_customer_says_they_made():
    from datetime import datetime, timedelta

    from domain import orders as rules
    from domain import receipts as gc

    talk = datetime(2026, 10, 16, 16, 7, tzinfo=gc.PHT)  # the customer asks about the product
    msg = lambda id, minutes: {"id": id, "created_time": (talk + timedelta(minutes=minutes)).isoformat()}
    order = {
        "status": "confirmed",
        "version": 1,
        "draft": {"total": 50000, "items": [{"sources": ["m2", "m4"]}], "customer_name": {"value": "Mark", "sources": ["m3"]}},
        "messages": [msg("m0", -60), msg("m1", 0), msg("m2", 1), msg("m3", 2), msg("m4", 3), msg("m5", 4)],
    }
    assert gc.conversation_start(order) == talk + timedelta(minutes=1)  # earliest cited message; the uncited "hi" an hour earlier does not open it
    assert gc.conversation_start({**order, "draft": {"total": 50000}}) == talk - timedelta(minutes=60)  # nothing cited: the first message
    with pytest.raises(rules.Refused):
        gc.conversation_start({**order, "messages": []})

    pay_to = {"name": "Juan Dela Cruz", "mobile": "639171234567"}
    later = talk + timedelta(hours=1)
    m = gc.request(order, pay_to, "me", later, says_paid=True)["manual_payment"]
    assert (m["kind"], m["counts_from"], m["requested_at"]) == ("says_paid", talk + timedelta(minutes=1), later)
    assert gc.window(m) == (talk + timedelta(minutes=1), gc.BEGAN)

    # Asked first, then heard they had already paid: the window widens, the request time stays.
    asked = {**order, **gc.request(order, pay_to, "me", later)}
    assert gc.window(asked["manual_payment"]) == (later, gc.ASKED)
    widened = gc.request(asked, pay_to, "me", later + timedelta(minutes=5), says_paid=True)["manual_payment"]
    assert (widened["requested_at"], gc.window(widened)[0]) == (later, talk + timedelta(minutes=1))
    assert gc.request({**asked, "manual_payment": widened}, pay_to, "me", later, says_paid=True) == {}

    # A receipt paid before the seller pressed anything passes for says_paid, and fails for a plain request.
    receipt = gc.Receipt(
        readable=True,
        outcome="successful",
        amount_php=500.0,
        reference="1012345678901",
        paid_at="2026-10-16T16:10",
        recipient_name="JU** DE** C.",
        recipient_mobile="0917 *** 4567",
    )
    time = lambda m: next(
        c
        for c in gc.check_receipt(receipt, 50000, gc.window(m)[0], later + timedelta(minutes=10), pay_to, set(), since=gc.window(m)[1])
        if c["check"] == "time"
    )
    assert time(m)["result"] == "pass" and gc.BEGAN in time(m)["detail"]
    assert time(asked["manual_payment"])["result"] == "fail"
    older = receipt.model_copy(update={"paid_at": "2026-10-16T15:00"})  # an hour before the conversation: another purchase
    assert {c["check"]: c["result"] for c in gc.check_receipt(older, 50000, gc.window(m)[0], later, pay_to, set(), since=gc.BEGAN)}["time"] == "fail"


def test_gcash_qr_and_settings():

    from domain import orders as rules
    from domain import receipts as gc

    # Decoded from a real GCash "Receive money" QR.
    qr = "00020101021127830012com.p2pqrpay0111GXCHPHM2XXX02089996440303152170200000006560417DWQM4TK3JDNWF205D5204601653036085802PH5912KE**N G** S.6014BARANGGAY 61-A6104123463048CB7"
    assert gc.crc16(qr[:-4]) == "8CB7"
    assert gc.parse_qr(qr) == {"masked_name": "KE**N G** S.", "user_id": "DWQM4TK3JDNWF205D", "payload": qr}
    with pytest.raises(rules.Refused):
        gc.parse_qr(qr.replace("KE**N", "JO**N"))  # tampered: checksum no longer matches
    other_bank = qr.replace("GXCHPHM2XXX", "BNORPHMMXXX")[:-4]
    with pytest.raises(rules.Refused, match="not for a GCash"):
        gc.parse_qr(other_bank + gc.crc16(other_bank))
    with pytest.raises(rules.Refused):
        gc.parse_qr("https://example.com")

    saved = gc.settings(gc.Settings(name="Kevin Gil Soriano", mobile="0927 256 1234", qr=qr))
    assert saved["qr"]["masked_name"] == "KE**N G** S." and saved["mobile"] == "639272561234"
    with pytest.raises(rules.Refused, match="KE\\*\\*N"):
        gc.settings(gc.Settings(name="Juan Dela Cruz", mobile="0927 256 1234", qr=qr))  # typed name does not fit the QR
    assert gc.settings(gc.Settings(name="Kevin Soriano", mobile="09272561234"))["qr"] is None  # QR is optional


def test_gcash_history_export_is_parsed_exactly_and_matched():
    from datetime import datetime

    from domain import orders as rules
    from domain import receipts as gc

    # The layout of a real export (pypdf text), with made-up figures.
    text = """GCash Transaction History
2026-10-09 to 2026-10-15
Date and Time Description Reference No. Debit Credit Balance
STARTING BALANCE 1,000.00
2026-10-11 08:52 PM Payment to SHOP, Merchant Transaction Number: P1 0000000000001 100.00 900.00
2026-10-14 04:52 PM Received GCash from BPI / VYBE by BPI with account ending in 1111 andinvno:20261014BOPIPHMMXXXB000165215123456 1046000000001 1,250.37 2,150.37
2026-10-14 05:10 PM Received GCash from JU** DE** C. 1046000000002 83.00 2,233.37
ENDING BALANCE 2,233.37
Total Debit 100.00
Total Credit 1,333.37"""
    st = gc.parse_statement(text)
    assert [(c["reference"], c["amount"], c["invno"]) for c in st["credits"]] == [
        ("1046000000001", 125037, "20261014BOPIPHMMXXXB000165215123456"),
        ("1046000000002", 8300, None),
    ]  # spending is dropped
    assert st["start"] == datetime(2026, 10, 9, tzinfo=gc.PHT) and st["last_at"] == datetime(2026, 10, 14, 17, 10, tzinfo=gc.PHT)
    for broken in (
        text.replace("2,150.37", "2,150.73"),  # a digit read wrong breaks the running balance
        text.replace("2026-10-14 05:10 PM", "2026-10-14 05:10"),  # a row skipped breaks the ending balance
        "Some other PDF",
    ):
        with pytest.raises(rules.Refused):
            gc.parse_statement(broken)

    now = datetime(2026, 10, 15, tzinfo=UTC)
    sent = datetime(2026, 10, 14, 16, 0, tzinfo=gc.PHT)
    passing = [gc.check("amount", "pass", "")]
    bank_claim = {"id": "t", "reference": "123456", "receipt": {"paid_at": "2026-10-14T16:51"}, "checks": passing}
    order = {
        "status": "confirmed",
        "version": 1,
        "manual_payment": {"status": "claimed", "version": 1, "amount": 125037, "requested_at": sent, "claims": [bank_claim]},
    }
    run = lambda o, taken=frozenset(): gc.reconcile(o, st, "s1", set(taken), "me", now)

    # A bank screenshot whose 6 digits end the invno, with the exact amount: confirmed from the export.
    c = run(order)["manual_payment"]["confirmed"]
    assert (c["reference"], c["claim_id"], c["statement"]) == ("1046000000001", "t", "s1")
    assert run(order, {"1046000000001"}) == {}  # already confirmed on another order
    assert run({**order, "manual_payment": {**order["manual_payment"], "amount": 125000}}) == {}  # amount differs
    assert run({**order, "manual_payment": {**order["manual_payment"], "requested_at": now}}) == {}  # arrived before the request
    assert run({**order, "manual_payment": {**order["manual_payment"], "claims": []}}) == {}  # nothing points to the row
    seller_read = {**order["manual_payment"], "claims": [], "history_read": {"reference": "1046000000001", "image": {"path": "h"}}}
    assert run({**order, "manual_payment": seller_read})["manual_payment"]["confirmed"]["history_image"] == {"path": "h"}

    # Confirmed by hand: found, a different amount, missing although the export covers it, or outside the export.
    done = lambda ref, at=datetime(2026, 10, 14, 8, 30, tzinfo=UTC), amount=125037: {
        **order,
        "manual_payment": {**order["manual_payment"], "status": "seller_confirmed", "amount": amount, "confirmed": {"reference": ref, "at": at}},
    }
    assert run(done("1046000000001"))["manual_payment"]["history_check"]["status"] == "found"
    assert run(done("1046000000001", amount=8300))["manual_payment"]["history_check"]["status"] == "amount_differs"
    assert run(done("1046999999999"))["manual_payment"]["history_check"]["status"] == "not_found"
    assert run(done("1046999999999", at=now)) == {}  # confirmed after the export ends: it may simply not be in it yet
    assert run({**order, "version": 2}) == {}  # the request is for an older version


def test_sample_gcash_files_go_through_the_real_checks():
    from datetime import datetime, timedelta

    from domain import receipts as gc
    from seed import samples

    # The sample receipt pays the demo account; the wrong-recipient one does not.
    assert gc.name_fits(samples.PAY_TO_NAME, config.DEMO_GCASH["name"]) and not gc.name_fits(samples.OTHER_NAME, config.DEMO_GCASH["name"])
    assert gc.mobile_fits(gc.mobile(samples.PAY_TO_MOBILE), config.DEMO_GCASH["mobile"])
    assert not gc.mobile_fits(gc.mobile(samples.OTHER_MOBILE), config.DEMO_GCASH["mobile"])
    assert samples.sender("Ana Reyes") == "AN* R." and len(samples.new_reference()) == 13
    assert samples.receipt_png(36000, "1012345678901", datetime.now(gc.PHT), samples.PAY_TO_NAME, samples.PAY_TO_MOBILE)[:4] == b"\x89PNG"
    assert samples.history_png(36000, "1012345678901", datetime.now(gc.PHT), "AN* R.")[:4] == b"\x89PNG"

    # The history PDF opens with the demo password and parses exactly, running balance and all.
    now = datetime.now(gc.PHT)
    sent = [{"at": now, "reference": "1012345678901", "amount": 36000, "sender": "AN* R."}]
    pdf = samples.statement_pdf(samples.statement_lines(sent, (now - timedelta(days=1)).replace(hour=0, minute=0), now))
    st = gc.parse_statement(gcash_routes.statement_text(pdf, samples.PASSWORD))
    assert ("1012345678901", 36000) in [(c["reference"], c["amount"]) for c in st["credits"]]
    with pytest.raises(HTTPException) as wrong:
        gcash_routes.statement_text(pdf, "wrong")
    assert wrong.value.status_code == 422

    # Each demo visitor's receipts are indexed apart from the store's and from each other's.
    assert gcash_routes.index_scope({"source": {"type": "messenger"}}) == ""
    assert gcash_routes.index_scope({"source": {"type": "demo", "owner": "u1"}}) != gcash_routes.index_scope(
        {"source": {"type": "demo", "owner": "u2"}}
    )

def test_model_armor_findings_become_checks_and_not_screened_is_never_a_pass():
    from domain import receipts as gcash
    from domain import signals as risk
    from integrations import armor

    ran = lambda match: {"executionState": "EXECUTION_SUCCESS", "matchState": match}
    result = {
        "filterResults": {
            "pi_and_jailbreak": {"piAndJailbreakFilterResult": ran("MATCH_FOUND")},
            "malicious_uris": {"maliciousUriFilterResult": ran("NO_MATCH_FOUND")},
            "rai": {"raiFilterResult": ran("MATCH_FOUND")},  # a filter this app does not use is ignored
        }
    }
    assert armor.findings(result) == ["instructions"]
    skipped = {"filterResults": {"pi_and_jailbreak": {"piAndJailbreakFilterResult": {"executionState": "EXECUTION_SKIPPED"}}}}
    assert armor.findings(skipped) is None

    msgs = [{"id": "c1", "from_page": False, "created_time": "2026-10-18T01:00:00+00:00"}]
    got = risk.screened({"c1": ["instructions", "unsafe_link"], "c2": ["instructions"], "c3": []}, msgs)  # c2 is not in this chat
    assert [(s["kind"], s["sources"], s["times"]) for s in got] == [
        ("instructions", ["c1"], ["2026-10-18T01:00:00+00:00"]),
        ("unsafe_link", ["c1"], ["2026-10-18T01:00:00+00:00"]),
    ]
    assert all(s["check"] == risk.SUGGESTED[s["kind"]] for s in got)
    assert all(s["resolved"] is None for s in risk.resolve(got, [{"status": "confirmed", "payment": {"status": "verified", "amount": 1}}]))

    assert gcash.screening(None) == []
    assert [c["result"] for c in gcash.screening([])] == ["pass"]
    assert [c["result"] for c in gcash.screening(["instructions"])] == ["fail"]

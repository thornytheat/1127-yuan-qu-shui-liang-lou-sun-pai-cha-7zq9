from meterloss.delivery import MODULES, application


def test_real_overview_returns_operational_evidence():
    response = application.test_client().get("/api/overview")
    assert response.status_code == 200
    model = response.get_json()
    assert len(model["run_id"]) == 12
    assert 4 <= len(model["metrics"]) <= 6
    assert len(model["stages"]) == len(MODULES)
    assert len(model["rows"]) >= 6
    assert all(model["columns"] for row in model["rows"] if row)
    assert len(model["series"]) >= 5


def test_scenario_result_and_stage_use_same_snapshot():
    client = application.test_client()
    response = client.post("/api/scenario", json={"capacity": 3})
    assert response.status_code == 200
    result = response.get_json()
    stage = client.get(f"/api/stages/{MODULES[-1][0]}?capacity=3").get_json()
    assert stage == {**result["stages"][-1], "run_id": result["run_id"]}


def test_malformed_capacity_and_unknown_stages_are_explicit():
    client = application.test_client()
    for data in ({"capacity": True}, {"capacity": 0}, {"capacity": 101}, {}, [1]):
        response = client.post("/api/scenario", json=data)
        assert response.status_code == 400
        assert response.get_json()["code"] == "INVALID_SCENARIO"
    assert client.get("/api/stages/unknown").get_json()["code"] == "UNKNOWN_STAGE"


def test_health_page_and_local_assets():
    client = application.test_client()
    assert client.get("/health").get_json()["service"] == "meterloss"
    body = client.get("/").get_data(as_text=True)
    assert "园区水量漏损排查" in body and "theme.css" in body
    assert client.get("/static/workbench.js").status_code == 200


def test_repeated_scenario_has_the_same_run_fingerprint():
    client = application.test_client()
    first = client.post("/api/scenario", json={"capacity": 5}).get_json()
    second = client.post("/api/scenario", json={"capacity": 5}).get_json()
    assert first == second


def test_large_request_returns_structured_error():
    response = application.test_client().post("/api/scenario", json={"capacity": "X" * 9000})
    assert response.status_code == 413
    assert response.get_json()["code"] == "REQUEST_TOO_LARGE"


def test_workbench_returns_next_shift_lines_and_tickets():
    model = application.test_client().get("/api/overview").get_json()
    assert model["alert_lines"] and model["dispatch_tickets"]
    assert model["line_version"] and all(row["line_version"] == model["line_version"]
                                         for row in model["alert_lines"])
    assert set(model["line_budget"]) == {"before", "after", "capacity"}
    body = application.test_client().get("/").get_data(as_text=True)
    assert "下一班预警线" in body and "下一班工单" in body


def test_dispatch_replay_endpoint_reports_no_new_tickets():
    client = application.test_client()
    first = client.post("/api/scenario", json={"capacity": 6}).get_json()
    replay = client.post("/api/dispatch", json={"capacity": 6, "issued": first["dispatch_tickets"]})
    assert replay.status_code == 200
    body = replay.get_json()
    assert body["metrics"]["新增工单"] == 0
    assert body["dispatch_tickets"] == first["dispatch_tickets"]
    for data in ({"capacity": 6}, {"capacity": 6, "issued": "x"}, {"capacity": 0, "issued": []}):
        assert client.post("/api/dispatch", json=data).status_code == 400

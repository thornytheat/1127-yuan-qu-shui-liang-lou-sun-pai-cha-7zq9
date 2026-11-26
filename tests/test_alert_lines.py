import pytest
from meterloss.loss_screen.alert_lines import (
    ReplayCase, _candidate, calibrate, line_lookup, parse_receipt, replay_cases,
    split_receipts,
)
from meterloss.loss_screen.scoring import latest_signal
from meterloss.pipeline import run


def receipt(ticket="XH-1", meter="ZF-801", closed=8800, fault=True):
    return {"ticket": ticket, "meter": meter, "closed": closed, "fault": fault}


COHORTS = {"ZF-801": {"cohort": "A"}, "ZF-802": {"cohort": "A"}, "ZF-803": {"cohort": "B"}}


def test_candidate_lowers_between_excluded_and_confirmed():
    # 放低门槛要严格高于已排除样本，同时不高于最弱的已确认漏点。
    line, direction, note = _candidate(1.1, confirmed=[2.0, 5.0], excluded=[0.4])
    assert direction == "下调" and line > 0.4 and line <= 2.0
    assert "已排除样本之上" in note


def test_candidate_keeps_line_when_no_replay_signal():
    assert _candidate(1.1, [], []) == (1.1, "沿用", "没有可用结案信号，延续现预警线")


def test_candidate_refuses_to_cross_overlapping_samples():
    # 排除样本比确认样本还高，无法分离，不能凭空改线。
    assert _candidate(1.1, confirmed=[0.5], excluded=[0.9])[0] == 1.1


def test_candidate_only_confirmed_never_guesses_below_weakest_point():
    line, direction, _ = _candidate(3.0, confirmed=[1.2], excluded=[])
    assert line == 1.2 and direction == "下调"


def test_candidate_raises_only_when_no_confirmed_case_rules_it_out():
    line, direction, _ = _candidate(0.2, confirmed=[], excluded=[0.6])
    assert line > 0.6 and direction == "上调"
    # 排除样本高于唯一的确认样本，无法同时不误报又不漏报，维持原线。
    blocked, kept, _ = _candidate(0.2, confirmed=[0.55], excluded=[0.6])
    assert blocked == 0.2 and kept == "沿用"


def test_late_receipt_is_deferred_not_counted():
    # 结案晚于交班时点的回执只能在下一班生效。
    usable, deferred = split_receipts([receipt(closed=9010)], COHORTS, 9000)
    assert usable == []
    assert deferred[0]["ticket"] == "XH-1" and "下一班" in deferred[0]["reason"]


def test_receipt_for_unregistered_meter_is_reported_not_used():
    usable, deferred = split_receipts([receipt(meter="ZF-999")], COHORTS, 9000)
    assert usable == [] and deferred[0]["reason"] == "用水画像未建档"


def test_duplicate_receipt_ticket_is_rejected():
    with pytest.raises(ValueError, match="重复"):
        split_receipts([receipt(), receipt()], COHORTS, 9000)
    with pytest.raises(ValueError):
        parse_receipt({"ticket": "X", "meter": "ZF-801", "closed": 8800, "fault": 1})


def test_latest_signal_ignores_missing_nights():
    projection = {"ZF-801": {"eligible": True, "nights": [
        {"night": "N1", "start": 100, "signal": 2.0},
        {"night": "N2", "start": 2000, "signal": None},
        {"night": "N3", "start": 4000, "signal": 6.0},
    ]}}
    assert latest_signal(projection, "ZF-801", 5000, True) == 2.0
    assert latest_signal(projection, "ZF-801", 5000, False) == 6.0
    assert latest_signal(projection, "ZF-801", 100, True) == 2.0
    empty = {"ZF-801": {"eligible": True, "nights": [{"night": "N1", "start": 0, "signal": None}]}}
    assert latest_signal(empty, "ZF-801") is None
    # 基线不足或偏离簇的表不进入自动判定，也不提供回放信号。
    weak = {"ZF-801": {"eligible": False, "nights": [{"night": "N1", "start": 0, "signal": 3.0}]}}
    assert latest_signal(weak, "ZF-801") is None


def cases():
    return (
        ReplayCase("T1", "ZF-801", "A", 8800, True, 0.4),
        ReplayCase("T2", "ZF-802", "A", 8810, False, 0.9),
        ReplayCase("T3", "ZF-803", "B", 8820, False, 2.5),
    )


def demand(lines):
    return {"A": 3 if lines.get("A", 1.1) <= 0.5 else 1,
            "B": 1 if lines.get("B", 1.1) <= 2.0 else 0}


def test_calibration_uses_confirmed_and_excluded_to_pick_line():
    decisions, budget = calibrate(cases(), "P1", 1.1, {"A": "生活", "B": "生产"}, 8, 9000, demand)
    by = line_lookup(decisions)
    assert by["A"].current > 0.9 and by["A"].current <= 1.1
    assert by["B"].direction == "上调" and by["B"].current > 2.5
    assert budget["capacity"] == 8


def test_lowered_line_is_withheld_when_it_would_fill_the_quota():
    # A 下调会从 1 单变成 3 单，名额只有 2 时本班不能放低。
    decisions, budget = calibrate(cases(), "P1", 1.1, {"A": "生活", "B": "生产"}, 2, 9000, demand)
    by = line_lookup(decisions)
    assert by["A"].direction == "沿用" and by["A"].current == 1.1
    assert budget["after"] <= budget["capacity"]


def test_every_cohort_has_one_decision_even_without_replay_cases():
    decisions, _ = calibrate((), "P1", 1.1, {"A": "生活", "B": "生产"}, 6, 9000, demand)
    assert [row.cohort for row in decisions] == ["A", "B"]
    assert all(row.direction == "沿用" and row.current == 1.1 for row in decisions)


def test_calibration_version_is_shared_by_all_cohorts():
    decisions, _ = calibrate(cases(), "P1", 1.1, {"A": "生活", "B": "生产"}, 8, 9000, demand)
    assert len({row.version for row in decisions}) == 1
    other, _ = calibrate(cases(), "P2", 1.1, {"A": "生活", "B": "生产"}, 8, 9000, demand)
    assert decisions[0].version != other[0].version


def test_replay_cases_keep_missing_signal_unsaid():
    rows = replay_cases([{"ticket": "T", "meter": "ZF-801", "closed": 8800,
                          "confirmed": True, "cohort": "A"}], lambda meter, closed, fault: None)
    assert rows[0].signal is None


def test_sample_round_binds_every_meter_to_one_line_version():
    result = run()
    version = result["line_version"]
    assert version and all(row["line_version"] == version for row in result["alert_lines"])
    # 每张表的预警线来自它所属用水簇，且派次依据与预警线同版。
    line_of = {row["cohort"]: row["adopted_line"] for row in result["alert_lines"]}
    for row in result["rows"]:
        assert row["line"] == line_of[row["cluster"]]


def test_sample_late_receipt_only_affects_next_shift():
    result = run()
    deferred = {row["ticket"] for row in result["replay"]["deferred"]}
    assert "XH-120" in deferred
    assert result["replay"]["cases"] == 6


def test_rerunning_same_receipts_opens_no_new_tickets():
    first = run()
    assert first["metrics"]["新增工单"] > 0
    again = run(issued=first["dispatch_tickets"])
    assert again["metrics"]["新增工单"] == 0
    assert again["dispatch_tickets"] == first["dispatch_tickets"]

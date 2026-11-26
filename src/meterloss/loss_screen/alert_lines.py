"""按用水簇标定下一班的漏损预警线。

结案复测只作为回放样本，不回头修改当班清单：``cutoff`` 之前已结案的回执允许进入
回放，``cutoff`` 之后才结案的回执属于迟到记录，只能从下一班开始生效。

每个用水簇单独确定下一班的净余流预警线（升每分钟），判定口径与漏损筛查一致：

- 排除样本给出可行下界——预警线必须高于已确认的非漏点，避免重复误报；
- 确认样本给出上界——预警线不得高于最弱的已确认漏点，否则会漏掉它；
- 在可行区间内取最靠近下界的一条线，尽量放低门槛少漏；
- 放低门槛会新增听漏工单，名额有限时按证据强度逐簇启用，其余继续沿用。

每张表采用哪条线、为什么改线、工单依据都随同一版线记录，重跑同一批回执不会另开
工单（见 ``field_rounds.ticketing``）。
"""
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from math import floor, isfinite


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """一条可用于回放的已结案复测记录。

    ``signal`` 是判定口径下的净余流：确认漏点取它最弱的一晚（预警线须低于它才不
    漏判），排除样本取它最强的一晚（预警线须高于它才不重复误报）。缺测夜窗没有
    信号，记为 ``None``，既不能算确认也不能算排除。
    """

    ticket: str
    meter: str
    cohort: str
    closed: int
    confirmed: bool
    signal: float | None


@dataclass(frozen=True, slots=True)
class LineDecision:
    cohort: str
    usage: str
    previous: float
    current: float
    changed: bool
    direction: str
    confirmed: int
    excluded: int
    unjudged: int
    missed: int
    false_calls: int
    dispatches_before: int
    dispatches_after: int
    version: str
    reason: str

    def as_record(self) -> dict:
        return {
            'cohort': self.cohort,
            'usage': self.usage,
            'previous_line': self.previous,
            'adopted_line': self.current,
            'changed': self.changed,
            'direction': self.direction,
            'confirmed_cases': self.confirmed,
            'excluded_cases': self.excluded,
            'unjudged_cases': self.unjudged,
            'missed_cases': self.missed,
            'false_calls': self.false_calls,
            'dispatches_before': self.dispatches_before,
            'dispatches_after': self.dispatches_after,
            'line_version': self.version,
            'reason': self.reason,
        }


def parse_receipt(raw):
    try:
        ticket = raw["ticket"].strip()
        meter = raw["meter"].strip()
        closed = raw["closed"]
        fault = raw["fault"]
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"复测回执缺少可用字段 {error}") from error
    if not ticket or not meter:
        raise ValueError("复测回执缺少工单号或表号")
    if type(closed) is not int or not 0 <= closed <= 10080:
        raise ValueError("复检分钟越界")
    if type(fault) is not bool:
        raise ValueError("复测结论须为布尔值")
    return ticket, meter, closed, fault


def split_receipts(receipts, cohorts, cutoff):
    """按结案时点把回执切成可回放与迟到两批。

    ``cutoff`` 是本次交班允许看到的结案时点。晚于它的回执不能在本班生效，
    只能留到下一班，因此既不参与确认也不参与排除。
    """
    if type(cutoff) is not int or not 0 <= cutoff <= 10080:
        raise ValueError("回放结案时点越界")
    usable = []
    deferred = []
    seen = set()
    for raw in receipts:
        ticket, meter, closed, fault = parse_receipt(raw)
        if closed > cutoff:
            deferred.append({"ticket": ticket, "meter": meter, "closed": closed,
                             "reason": "迟到回执，下一班生效"})
            continue
        if ticket in seen:
            raise ValueError("复测工单编号重复")
        seen.add(ticket)
        info = cohorts.get(meter)
        if info is None:
            deferred.append({"ticket": ticket, "meter": meter, "closed": closed,
                             "reason": "用水画像未建档"})
            continue
        usable.append({"ticket": ticket, "meter": meter, "closed": closed,
                       "confirmed": fault, "cohort": info["cohort"]})
    usable.sort(key=lambda row: (row["closed"], row["ticket"]))
    deferred.sort(key=lambda row: (row["closed"], row["ticket"]))
    return usable, deferred


def replay_cases(usable, signal_of):
    """把可用回执绑定到结案前可用夜窗的判定信号。"""
    cases = []
    for row in usable:
        value = signal_of(row["meter"], row["closed"], row["confirmed"])
        cases.append(ReplayCase(row["ticket"], row["meter"], row["cohort"], row["closed"],
                                row["confirmed"],
                                round(value, 6) if value is not None and isfinite(value) else None))
    return tuple(cases)


def _ceil6(value):
    """向上取到六位小数，确保严格高于排除样本。"""
    return (floor(value * 1_000_000) + 1) / 1_000_000


def _candidate(previous, confirmed, excluded):
    """在确认/排除样本给出的可行区间内取最低预警线。

    返回 ``(线, 方向, 说明)``。当样本不足以划出可行区间时沿用现预警线。
    """
    if not confirmed and not excluded:
        return previous, "沿用", "没有可用结案信号，延续现预警线"
    lower = max(excluded) if excluded else None
    upper = min(confirmed) if confirmed else None
    if lower is not None and upper is not None and lower >= upper:
        return previous, "沿用", "确认与排除样本交叠，维持原预警线"
    if lower is None:
        # 只有确认样本：最多下调到最弱的已确认漏点，不猜测更低的门槛。
        if upper < previous:
            return max(0.0, upper), "下调", "下调到最弱已确认漏点，全部确认样本均可覆盖"
        return previous, "沿用", "现预警线已覆盖全部确认样本"
    target = _ceil6(lower)
    if upper is not None and target >= upper:
        # 可行区间窄于六位小数，退回区间中点；仍无空间则沿用。
        target = round((lower + upper) / 2, 6)
        if not lower < target < upper:
            return previous, "沿用", "确认与排除样本过近，维持原预警线"
    if upper is not None and target > upper:
        return previous, "沿用", "确认与排除样本过近，维持原预警线"
    if target < 0:
        # 负的净余流阈值没有业务含义，最低到零。
        return 0.0, "下调", "放低到零，减少漏判且不重复误报"
    if target < previous:
        return target, "下调", "放低到已排除样本之上，减少漏判且不重复误报"
    if target > previous:
        return target, "上调", "现预警线低于已排除样本，抬高以避免重复误报"
    return previous, "沿用", "现预警线已在可行区间内"


def _apply_version(rows, policy_version, cutoff):
    payload = [policy_version, cutoff,
               sorted((row["cohort"], round(row["current"], 9)) for row in rows)]
    return sha256(dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def calibrate(cases, policy_version, previous, usage_names, capacity, cutoff, count_alerts):
    """确定下一班每个用水簇的预警线。

    ``count_alerts(lines)`` 返回 ``{cohort: 会占用巡检名额的工单数}``。放低门槛的
    簇按证据强度排序，逐个试着启用；启用后总名额超限就退回沿用。返回按簇编号排序
    的 ``LineDecision`` 元组。
    """
    if type(capacity) is not int or not 1 <= capacity <= 100:
        raise ValueError("巡检容量须在 1 至 100")
    if not 0 <= cutoff <= 10080:
        raise ValueError("回放结案时点越界")
    if not isfinite(previous) or previous < 0:
        raise ValueError("现预警线非法")
    names = usage_names or {}
    grouped = {}
    for row in cases:
        grouped.setdefault(row.cohort, []).append(row)
    # 每个簇都要给出下一班采用的线，没有回放样本的簇也记一条「沿用」。
    draft = []
    for cohort in sorted(set(grouped) | set(names)):
        rows = grouped.get(cohort, [])
        confirmed = [row.signal for row in rows if row.confirmed and row.signal is not None]
        excluded = [row.signal for row in rows if not row.confirmed and row.signal is not None]
        unjudged = sum(1 for row in rows if row.signal is None)
        target, direction, note = _candidate(float(previous), confirmed, excluded)
        draft.append({
            "cohort": cohort, "usage": names.get(cohort, "未标注"),
            "previous": float(previous), "target": float(target), "direction": direction,
            "confirmed": len(confirmed), "excluded": len(excluded), "unjudged": unjudged,
            "missed": sum(value < previous for value in confirmed),
            "false_calls": sum(value >= previous for value in excluded),
            "reason": note,
        })

    adopted = {row["cohort"]: row["previous"] for row in draft}
    per_cohort_before = count_alerts(adopted)
    total_before = sum(per_cohort_before.values())
    # 先启用证据最强的下调：确有漏判的簇优先，其次确认样本多的簇。
    lowering = sorted((row for row in draft if row["direction"] == "下调"),
                      key=lambda row: (-row["missed"], -row["confirmed"], row["cohort"]))
    for row in lowering:
        trial = {**adopted, row["cohort"]: row["target"]}
        if sum(count_alerts(trial).values()) <= capacity:
            adopted = trial
        else:
            row["direction"] = "沿用"
            row["target"] = row["previous"]
            row["reason"] = "放低门槛会超出听漏名额，本班继续沿用"
    per_cohort_after = count_alerts(adopted)
    total_after = sum(per_cohort_after.values())
    if total_after > capacity:
        # 沿用现有预警线也已超出名额时，改线不能进一步放大占用，全部回到沿用。
        for row in draft:
            if row["direction"] != "沿用":
                row["direction"] = "沿用"
                row["target"] = row["previous"]
                row["reason"] = "沿用现有预警线已占满听漏名额，本班不改线"
        adopted = {row["cohort"]: row["previous"] for row in draft}
        per_cohort_after = per_cohort_before
        total_after = total_before

    prepared = [{
        "cohort": row["cohort"], "usage": row["usage"], "previous": row["previous"],
        "current": round(row["target"], 6) if row["target"] != row["previous"] else row["previous"],
        "changed": row["target"] != row["previous"], "direction": row["direction"],
        "confirmed": row["confirmed"], "excluded": row["excluded"], "unjudged": row["unjudged"],
        "missed": row["missed"], "false_calls": row["false_calls"],
        "dispatches_before": per_cohort_before.get(row["cohort"], 0),
        "dispatches_after": per_cohort_after.get(row["cohort"], 0),
        "reason": row["reason"],
    } for row in draft]
    version = _apply_version(prepared, policy_version, cutoff)
    decisions = tuple(LineDecision(
        cohort=row["cohort"], usage=row["usage"], previous=row["previous"], current=row["current"],
        changed=row["changed"], direction=row["direction"], confirmed=row["confirmed"],
        excluded=row["excluded"], unjudged=row["unjudged"], missed=row["missed"],
        false_calls=row["false_calls"], dispatches_before=row["dispatches_before"],
        dispatches_after=row["dispatches_after"], version=version, reason=row["reason"])
        for row in prepared)
    return decisions, {"before": total_before, "after": total_after, "capacity": capacity}


def line_lookup(decisions):
    return {row.cohort: row for row in decisions}

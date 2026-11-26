"""下一班巡检工单编号与重跑去重。

工单号由这条指令本身的业务内容决定：表号、任务、班组、开始分钟、工单状态，以及
它采用的预警线与预警线版本。因此：

- 同一批回执重跑，每条指令都命中同一个工单号，不会新开一张工单；
- 迟到的复测回执只改变相关表的判定，其余表的工单号保持不变，不会整批换号重开；
- 预警线换版后，派次依据改变，相关工单会得到新号，与新线同版。

``merge_tickets`` 返回的 ``new_count`` 即本批相对已发台账真正新增的工单数。
"""
from hashlib import sha256
from json import dumps


def batch_fingerprint(receipts, capacity, policy_version, line_version):
    """同一批回执 + 同一配额 + 同一预警线版本得到同一批次指纹。"""
    if type(capacity) is not int or not 1 <= capacity <= 100:
        raise ValueError("巡检容量须在 1 至 100")
    rows = []
    for raw in receipts:
        try:
            rows.append((str(raw["ticket"]).strip(), str(raw["meter"]).strip(),
                         raw["closed"], raw["fault"]))
        except (KeyError, TypeError) as error:
            raise ValueError(f"复测回执缺少可用字段 {error}") from error
    payload = [sorted(rows), capacity, policy_version, line_version]
    return sha256(dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def ticket_number(work, line_version, alert_line):
    payload = [work.meter, work.task, work.crew or "", work.from_minute, work.state,
               line_version, alert_line]
    return "WO-" + sha256(dumps(payload, ensure_ascii=False).encode()).hexdigest()[:10].upper()


def compose_tickets(work, line_version, line_of):
    """给本轮需要出车的巡检指令附上稳定工单号与同版线依据。

    归档记录不出车，不占用工单；下一轮与值班协调的指令保留在台账里，便于下一班
    接着处理。
    """
    tickets = []
    for row in work:
        if row.task == "归档":
            continue
        line = line_of.get(row.meter)
        tickets.append({
            "ticket": ticket_number(row, line_version, line),
            "meter": row.meter,
            "sector": row.sector,
            "task": row.task,
            "crew": row.crew,
            "state": row.state,
            "from_minute": row.from_minute,
            "to_minute": row.to_minute,
            "minutes": row.minutes,
            "alert_line": line,
            "line_version": line_version,
        })
    return tuple(tickets)


def merge_tickets(existing, proposed):
    """把新算出的工单并入已发工单台账，按工单号去重。

    已存在的工单保留原记录，重跑不会改写班组与时段；``new_count`` 为本批真正
    新增的工单数，同一批回执重跑时应为零。
    """
    ledger = {}
    for row in existing:
        try:
            key = row["ticket"]
        except (KeyError, TypeError) as error:
            raise ValueError(f"已发工单缺少编号 {error}") from error
        if key in ledger:
            raise ValueError("已发工单编号重复")
        ledger[key] = dict(row)
    added = 0
    for row in proposed:
        key = row["ticket"]
        if key in ledger:
            continue
        ledger[key] = dict(row)
        added += 1
    return tuple(ledger[key] for key in sorted(ledger)), added

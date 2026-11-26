from collections import defaultdict
from math import sqrt
from statistics import median
from .contracts import Track

def normalize(rate, pressure, anchoring):
    return rate * sqrt(anchoring/max(50,pressure)) if rate is not None else None

def normalized_rate(entry, anchoring):
    return normalize(entry.rate,entry.pressure,anchoring)

def tail_probability(reference, observed):
    if not reference:
        return None
    return (1+sum(x>=observed for x in reference))/(len(reference)+1)

def meters_by_name(records):
    pairs=defaultdict(list)
    for row in records:
        pairs[row.meter].append(row)
    return {key:sorted(rows,key=lambda row:row.start) for key,rows in sorted(pairs.items())}

def _enough(group, baseline, policy):
    return len(baseline)>=policy.minimum_samples and group["distance"]<=policy.cohort_reject

def _floor(baseline, group):
    return median(baseline) if baseline else group["typical_lpm"]

def project(history, recent, cohorts, policy):
    """把每张表的目标夜窗折算成逐夜净余流信号。

    缺测或不可比夜窗记 ``None``，不按正常用水填补。``eligible`` 表示该表具备
    进入自动判定的基线样本且没有偏离画像簇；缺测夜窗不改变这一资格判断，只在
    出结论时要求补采。
    """
    prior=meters_by_name(history)
    projection={}
    for meter, daily in meters_by_name(recent).items():
        if meter not in cohorts:
            projection[meter]={"meter":meter,"sector":daily[0].sector,"cohort":"未登记",
                               "distance":None,"floor":None,"baseline_count":0,
                               "eligible":False,"nights":[],"isolated":None}
            continue
        group=cohorts[meter]
        baseline=[normalized_rate(entry,policy.pressure_anchor) for entry in prior.get(meter,())]
        baseline=[value for value in baseline if value is not None]
        floor=_floor(baseline,group)
        nights=[]
        for entry in daily:
            value=normalized_rate(entry,policy.pressure_anchor)
            usable=value is not None and entry.excluded is None
            nights.append({"night":entry.night,"start":entry.start,
                           "signal":round(value-floor,6) if usable else None,
                           "missing":entry.excluded or ("覆盖不足" if value is None else None)})
        projection[meter]={"meter":meter,"sector":daily[0].sector,"cohort":group["cohort"],
                           "distance":group["distance"],"floor":round(floor,6),
                           "baseline_count":len(baseline),"eligible":_enough(group,baseline,policy),
                           "nights":nights,
                           "isolated":max((entry.test_rate for entry in daily
                                           if entry.test_rate is not None),default=None)}
    return projection

def latest_signal(projection, meter, closed=None, confirmed=True):
    """回放判定信号：确认漏点取可用夜窗中最弱一晚，排除样本取最强一晚。

    缺测夜窗没有信号，不参与回放；基线过少或偏离画像簇的表不进入自动判定，
    同样不给回放信号，只能人工复核。返回 ``None`` 表示本表无法回放。
    """
    row=projection.get(meter)
    if row is None or not row["eligible"]:
        return None
    values=[entry["signal"] for entry in row["nights"]
            if entry["signal"] is not None and (closed is None or entry["start"]<=closed)]
    if not values:
        return None
    return min(values) if confirmed else max(values)

def screen(history, recent, cohorts, policy, lines=None):
    """逐表判定漏损分级。``lines`` 是各簇下一班预警线；缺省沿用策略最小余流。"""
    thresholds=lines or {}
    prior=meters_by_name(history)
    diagnostics=[]
    for meter, daily in meters_by_name(recent).items():
        if meter not in cohorts:
            diagnostics.append(Track(meter,daily[0].sector,"资料缺失",len(daily),0,0,None,0,"未登记","用水画像未建档",None))
            continue
        group=cohorts[meter]
        line=float(thresholds.get(group["cohort"],policy.minimum_rate))
        features=[normalized_rate(entry,policy.pressure_anchor) for entry in prior.get(meter,())]
        baseline=[value for value in features if value is not None]
        values=[(normalized_rate(entry,policy.pressure_anchor),entry) for entry in daily if not entry.excluded]
        good=[(value,row) for value,row in values if value is not None]
        floor=_floor(baseline,group)
        maximum=max((value for value,_ in good),default=floor)
        excess=max(0, maximum-floor)
        # 超过阈值的最长连续夜数，缺测或不可比夜窗会打断连续。
        persistence=0
        longest=0
        prior_start=None
        signal={r.start:v for v,r in good}
        for entry in daily:
            value=signal.get(entry.start)
            if value is None or value-floor<line:
                persistence=0
            else:
                persistence=1 if prior_start is None or entry.start-prior_start>1500 else persistence+1
            longest=max(longest,persistence)
            prior_start=entry.start
        pvalue=tail_probability(baseline,maximum) if good else None
        isolated=max((row.test_rate for _,row in good if row.test_rate is not None),default=None)
        # 缺测夜窗（无读数或覆盖不足）不能按正常用水处理，未触发告警时须要求补采。
        missing=sum(1 for entry in daily if entry.rate is None or entry.excluded=="覆盖不足")
        if not good:
            state,reason="需补采","目标窗口全部不可用于判断"
        elif isolated is not None and isolated>=policy.isolation_alarm:
            state,reason="关阀仍过水","隔离试验余流超过阈值"
        elif not _enough(group,baseline,policy):
            state,reason="人工复核","基线过少或偏离画像簇"
        elif longest>=policy.minimum_persistence and pvalue <= policy.calibration_cap:
            state,reason="疑似持续漏损","连续夜窗净余流及经验尾概率同时触发"
        elif missing:
            state,reason="需补采","存在缺测夜窗，不能按正常用水判定"
        else:
            state,reason="观察","未同时满足持续夜数及余流标准"
        diagnostics.append(Track(meter,daily[0].sector,state,len(daily),len(good),round(excess,4),
                                 round(pvalue,5) if pvalue is not None else None,longest,group["cohort"],reason,
                                 round(isolated,4) if isolated is not None else None,
                                 round(line,6),group.get("line_version",""),round(floor,6)))
    return tuple(diagnostics)

LISTENING={"关阀仍过水","疑似持续漏损"}

def listening_demand(history, recent, cohorts, policy, lines):
    """按候选预警线复算，返回各簇会被派往听漏的表数，用于有限名额分配。"""
    counts=defaultdict(int)
    for row in screen(history,recent,cohorts,policy,lines):
        if row.status in LISTENING:
            counts[row.cohort]+=1
    return dict(counts)


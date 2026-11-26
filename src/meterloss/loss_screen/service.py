from dataclasses import asdict
from .contracts import LossPolicy
from .scoring import screen,project,latest_signal,listening_demand

def assess(baseline,samples,usage,controls,lines=None):
    policy=LossPolicy.parse(controls)
    findings=screen(baseline,samples,usage,policy,lines)
    return policy,findings

def evidence(findings, policy):
    out=[]
    for item in findings:
        row=asdict(item)
        row["version"]=policy.version
        row["followup"]=item.status in {"关阀仍过水","疑似持续漏损","人工复核","需补采"}
        out.append(row)
    return out

def signals(history,samples,usage,controls):
    """逐表逐夜净余流证据，供标定下一班预警线使用。"""
    policy=LossPolicy.parse(controls)
    return policy,project(history,samples,usage,policy)

def dispatches(baseline,samples,usage,policy,lines):
    """按候选预警线复算，返回各簇会被派往听漏的表数。"""
    return listening_demand(baseline,samples,usage,policy,lines)

def replay_signal(projection):
    """把回放所需的判定信号封装成只读函数。"""
    return lambda meter,closed,confirmed: latest_signal(projection,meter,closed,confirmed)

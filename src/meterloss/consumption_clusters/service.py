from .contracts import ProfilePoint
from .medoids import fit,choose_group,best_overlap

def segment_profiles(history, registry, at):
    segmentation=fit(history,at)
    indexed={}
    for profile in registry:
        point=ProfilePoint.parse(profile)
        cohort,distance=choose_group(segmentation,point)
        indexed[point.key]={"meter":point.key,"sector":point.sector,"cohort":cohort,"distance":distance,
                            "typical_lpm":next(x.overnight_lpm for x in segmentation.medoids if x.key==cohort)}
    return segmentation, indexed

def cohort_usages(labels, cohorts):
    """按已标注水表为每个簇取出现最多的营业类型，作为下一班预警线的人读名称。"""
    tally={}
    for row in labels:
        info=cohorts.get(row["meter"])
        if info is None:
            continue
        bucket=tally.setdefault(info["cohort"],{})
        usage=row["usage"]
        bucket[usage]=bucket.get(usage,0)+1
    decided={}
    for cohort,votes in tally.items():
        decided[cohort]=min(votes,key=lambda usage:(-votes[usage],usage))
    return decided

# ui/learning.py
"""
System-Wide Learning — Background job for aggregating feedback.

Periodically scans the MongoDB feedback collection and:
  1. Aggregates accepted/rejected recommendations per depletion state
  2. Adjusts threshold recommendations based on farmer outcomes
  3. Tracks override patterns from scientists
  4. Logs learning events for auditability

Run standalone: python -m ui.learning
Or import and call run_learning_cycle() from a scheduler.

Phase 6 changes:
  - Removed unused ACTIVE_SOIL_TYPE import (was imported but never used).
"""
import time
from datetime import datetime, timezone
from pymongo import MongoClient
from shared.config import MONGO_URI, MONGO_DB

_client = MongoClient(MONGO_URI)
_db = _client[MONGO_DB]


def aggregate_feedback():
    """
    Process unprocessed feedback entries.
    Updates recommendation effectiveness scores.
    """
    unprocessed = list(_db.feedback.find({"processed": False}))
    if not unprocessed:
        return 0

    for entry in unprocessed:
        rec_id = entry.get("recommendation_id")
        accepted = entry.get("accepted", False)
        rating = entry.get("rating", 0)
        outcome = entry.get("outcome", "")

        # Store in learning summary
        _db.learning_summary.update_one(
            {"recommendation_id": rec_id},
            {
                "$set": {
                    "last_feedback": datetime.now(timezone.utc),
                    "accepted": accepted,
                    "rating": rating,
                    "outcome": outcome,
                },
                "$inc": {
                    "feedback_count": 1,
                    "positive_count": 1 if rating >= 3 else 0,
                    "negative_count": 1 if rating < 3 and rating > 0 else 0,
                },
            },
            upsert=True,
        )

        # Mark as processed
        _db.feedback.update_one(
            {"_id": entry["_id"]},
            {"$set": {"processed": True, "processed_at": datetime.now(timezone.utc)}},
        )

    return len(unprocessed)


def aggregate_threshold_overrides():
    """
    Analyze scientist threshold overrides and compute recommended adjustments.
    If a scientist repeatedly adjusts a threshold in the same direction,
    suggest updating the default.
    """
    pipeline = [
        {"$group": {
            "_id": {"soil_type": "$soil_type", "field": "$field"},
            "avg_new_warn": {"$avg": "$new_warn"},
            "avg_new_crit": {"$avg": "$new_crit"},
            "count": {"$sum": 1},
            "latest": {"$max": "$timestamp"},
        }},
        {"$match": {"count": {"$gte": 2}}},  # Only if overridden 2+ times
    ]

    results = list(_db.threshold_overrides.aggregate(pipeline))

    for r in results:
        _db.learning_threshold_suggestions.update_one(
            {"soil_type": r["_id"]["soil_type"], "field": r["_id"]["field"]},
            {
                "$set": {
                    "suggested_warn": round(r["avg_new_warn"], 2),
                    "suggested_crit": round(r["avg_new_crit"], 2),
                    "override_count": r["count"],
                    "last_override": r["latest"],
                    "updated_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )

    return len(results)


def compute_recommendation_effectiveness():
    """
    Compute per-state recommendation effectiveness based on outcomes.
    """
    pipeline = [
        {"$lookup": {
            "from": "recommendations",
            "localField": "recommendation_id",
            "foreignField": "diagnosis_id",
            "as": "rec",
        }},
        {"$unwind": {"path": "$rec", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$rec.state",
            "total": {"$sum": 1},
            "accepted": {"$sum": {"$cond": ["$accepted", 1, 0]}},
            "avg_rating": {"$avg": "$rating"},
            "worked": {"$sum": {"$cond": [{"$eq": ["$outcome", "worked"]}, 1, 0]}},
        }},
    ]

    try:
        results = list(_db.feedback.aggregate(pipeline))
        for r in results:
            if r["_id"]:
                _db.learning_effectiveness.update_one(
                    {"state": r["_id"]},
                    {
                        "$set": {
                            "total_feedback": r["total"],
                            "accepted_count": r["accepted"],
                            "acceptance_rate": round(r["accepted"] / max(r["total"], 1), 2),
                            "avg_rating": round(r.get("avg_rating", 0) or 0, 2),
                            "success_count": r["worked"],
                            "success_rate": round(r["worked"] / max(r["total"], 1), 2),
                            "updated_at": datetime.now(timezone.utc),
                        },
                    },
                    upsert=True,
                )
        return len(results)
    except Exception:
        return 0


def run_learning_cycle():
    """Run one complete learning cycle."""
    fb = aggregate_feedback()
    th = aggregate_threshold_overrides()
    eff = compute_recommendation_effectiveness()

    _db.learning_runs.insert_one({
        "timestamp": datetime.now(timezone.utc),
        "feedback_processed": fb,
        "threshold_suggestions": th,
        "effectiveness_computed": eff,
    })

    return {"feedback": fb, "thresholds": th, "effectiveness": eff}


def run():
    """Background loop — runs learning cycle every 5 minutes."""
    print("[LEARNING] System-wide learning job started. Cycle every 300s.")
    while True:
        try:
            result = run_learning_cycle()
            if any(v > 0 for v in result.values()):
                print(f"[LEARNING] Cycle complete: {result}")
        except Exception as e:
            print(f"[LEARNING] Error: {e}")
        time.sleep(300)


if __name__ == "__main__":
    run()
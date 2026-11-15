"""Synthetic local execution fixture; not the application backend."""

from threading import RLock


def new_activity(capacity):
    if capacity < 1:
        raise ValueError("capacity must be positive")
    return {"capacity": capacity, "sequence": 0, "records": {}, "lock": RLock()}


def join(activity, account):
    with activity["lock"]:
        previous = activity["records"].get(account)
        if previous and previous["state"] != "CANCELLED":
            return previous["state"]
        used = sum(r["state"] == "CONFIRMED" for r in activity["records"].values())
        state = "CONFIRMED" if used < activity["capacity"] else "WAITLISTED"
        activity["sequence"] += 1
        activity["records"][account] = {"state": state, "order": activity["sequence"]}
        return state


def cancel(activity, account):
    with activity["lock"]:
        record = activity["records"].get(account)
        if record is None or record["state"] == "CANCELLED":
            return "CANCELLED"
        released = record["state"] == "CONFIRMED"
        record["state"] = "CANCELLED"
        if released:
            waiting = [r for r in activity["records"].values() if r["state"] == "WAITLISTED"]
            if waiting:
                min(waiting, key=lambda r: r["order"])["state"] = "CONFIRMED"
        return "CANCELLED"


def query(activity, account):
    with activity["lock"]:
        record = activity["records"].get(account)
        return record["state"] if record else None

def validate_id(value):
    if not isinstance(value, str):
        return False
    if len(value) == 0:
        return False
    for ch in value:
        if not ((ch >= 'A' and ch <= 'Z') or (ch >= 'a' and ch <= 'z') or (ch >= '0' and ch <= '9') or ch == '-' or ch == '_'):
            return False
    return True


class LoanReport:
    def summarize(self, records):
        if not isinstance(records, list):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        for rec in records:
            if not isinstance(rec, dict):
                return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
            lid = rec.get("loan_id")
            eid = rec.get("equipment_id")
            st = rec.get("status")
            if not validate_id(lid) or not validate_id(eid) or not validate_id(st):
                return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        total = 0
        reserved = 0
        checked_out = 0
        returned = 0
        cancelled = 0
        for rec in records:
            total += 1
            st = rec["status"]
            if st == "RESERVED":
                reserved += 1
            elif st == "CHECKED_OUT":
                checked_out += 1
            elif st == "RETURNED":
                returned += 1
            elif st == "CANCELLED":
                cancelled += 1
        data = {
            "total": total,
            "reserved": reserved,
            "checked_out": checked_out,
            "returned": returned,
            "cancelled": cancelled
        }
        return {"ok": True, "data": data}

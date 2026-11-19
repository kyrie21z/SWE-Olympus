def validate_id(value):
    if not isinstance(value, str) or value == '':
        return False
    return True


class LoanService:
    def __init__(self, catalog):
        self.catalog = catalog
        self.loans = {}
        self.history_records = []

    def reserve(self, loan_id, equipment_id):
        if not validate_id(loan_id) or not validate_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if loan_id in self.loans:
            existing = self.loans[loan_id]
            if existing["equipment_id"] != equipment_id:
                return {"ok": False, "error": {"code": "CONFLICT"}}
            return {"ok": True, "data": {"loan_id": existing["loan_id"], "equipment_id": existing["equipment_id"], "status": existing["status"]}}
        check = self.catalog.get_equipment(equipment_id)
        if not check["ok"]:
            return {"ok": False, "error": {"code": check["error"]["code"]}}
        claim_result = self.catalog.claim(equipment_id)
        if not claim_result["ok"]:
            return {"ok": False, "error": {"code": claim_result["error"]["code"]}}
        record = {"loan_id": loan_id, "equipment_id": equipment_id, "status": "RESERVED"}
        self.loans[loan_id] = record
        self.history_records.append(record)
        return {"ok": True, "data": {"loan_id": record["loan_id"], "equipment_id": record["equipment_id"], "status": record["status"]}}

    def checkout(self, loan_id):
        if not validate_id(loan_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if loan_id not in self.loans:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        record = self.loans[loan_id]
        if record["status"] != "RESERVED":
            return {"ok": False, "error": {"code": "INVALID_TRANSITION"}}
        record["status"] = "CHECKED_OUT"
        return {"ok": True, "data": {"loan_id": record["loan_id"], "equipment_id": record["equipment_id"], "status": record["status"]}}

    def return_equipment(self, loan_id):
        if not validate_id(loan_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if loan_id not in self.loans:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        record = self.loans[loan_id]
        if record["status"] != "CHECKED_OUT":
            return {"ok": False, "error": {"code": "INVALID_TRANSITION"}}
        record["status"] = "RETURNED"
        self.catalog.release(record["equipment_id"])
        return {"ok": True, "data": {"loan_id": record["loan_id"], "equipment_id": record["equipment_id"], "status": record["status"]}}

    def cancel(self, loan_id):
        if not validate_id(loan_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if loan_id not in self.loans:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        record = self.loans[loan_id]
        if record["status"] != "RESERVED":
            return {"ok": False, "error": {"code": "INVALID_TRANSITION"}}
        record["status"] = "CANCELLED"
        self.catalog.release(record["equipment_id"])
        return {"ok": True, "data": {"loan_id": record["loan_id"], "equipment_id": record["equipment_id"], "status": record["status"]}}

    def history(self, equipment_id):
        if not validate_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        check = self.catalog.get_equipment(equipment_id)
        if not check["ok"]:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        result = []
        for record in self.history_records:
            if record["equipment_id"] == equipment_id:
                result.append({"loan_id": record["loan_id"], "equipment_id": record["equipment_id"], "status": record["status"]})
        return {"ok": True, "data": result}

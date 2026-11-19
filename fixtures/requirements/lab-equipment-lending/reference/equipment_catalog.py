def validate_equipment_id(equipment_id):
    if not isinstance(equipment_id, str) or equipment_id == '':
        return False
    return True


class EquipmentCatalog:
    def __init__(self):
        self.catalog = {}

    def register(self, equipment_id):
        if not validate_equipment_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if equipment_id in self.catalog:
            return {"ok": False, "error": {"code": "DUPLICATE"}}
        self.catalog[equipment_id] = "AVAILABLE"
        return {"ok": True, "data": {"equipment_id": equipment_id, "status": "AVAILABLE"}}

    def get_equipment(self, equipment_id):
        if not validate_equipment_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if equipment_id not in self.catalog:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        return {"ok": True, "data": {"equipment_id": equipment_id, "status": self.catalog[equipment_id]}}

    def claim(self, equipment_id):
        if not validate_equipment_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if equipment_id not in self.catalog:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        if self.catalog[equipment_id] == "UNAVAILABLE":
            return {"ok": False, "error": {"code": "UNAVAILABLE"}}
        self.catalog[equipment_id] = "UNAVAILABLE"
        return {"ok": True, "data": {"equipment_id": equipment_id, "status": "UNAVAILABLE"}}

    def release(self, equipment_id):
        if not validate_equipment_id(equipment_id):
            return {"ok": False, "error": {"code": "INVALID_ARGUMENT"}}
        if equipment_id not in self.catalog:
            return {"ok": False, "error": {"code": "NOT_FOUND"}}
        if self.catalog[equipment_id] == "AVAILABLE":
            return {"ok": False, "error": {"code": "INVALID_TRANSITION"}}
        self.catalog[equipment_id] = "AVAILABLE"
        return {"ok": True, "data": {"equipment_id": equipment_id, "status": "AVAILABLE"}}

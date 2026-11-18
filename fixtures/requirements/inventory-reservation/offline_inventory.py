from threading import RLock


def success(data):
    return {"ok": True, "data": dict(data)}


def failure(code):
    return {"ok": False, "error": {"code": code}}


def valid_id(value):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return isinstance(value, str) and 1 <= len(value) <= 32 and all(c in alphabet for c in value)


def valid_quantity(value, minimum):
    return type(value) is int and minimum <= value <= 10000


class InventoryService:
    def __init__(self):
        self.lock = RLock()
        self.skus = {}
        self.orders = {}

    def create_sku(self, sku, initial_stock):
        if not valid_id(sku) or not valid_quantity(initial_stock, 0):
            return failure("INVALID_ARGUMENT")
        with self.lock:
            if sku in self.skus:
                return failure("SKU_EXISTS")
            stock = {"sku": sku, "initial": initial_stock, "available": initial_stock, "reserved": 0, "sold": 0}
            self.skus[sku] = stock
            return success(stock)

    def get_sku(self, sku):
        if not valid_id(sku):
            return failure("INVALID_ARGUMENT")
        with self.lock:
            stock = self.skus.get(sku)
            return success(stock) if stock is not None else failure("SKU_NOT_FOUND")

    def get_order(self, order_id):
        if not valid_id(order_id):
            return failure("INVALID_ARGUMENT")
        with self.lock:
            order = self.orders.get(order_id)
            return success(order) if order is not None else failure("ORDER_NOT_FOUND")

    def reserve(self, order_id, sku, quantity):
        if not valid_id(order_id) or not valid_id(sku) or not valid_quantity(quantity, 1):
            return failure("INVALID_ARGUMENT")
        with self.lock:
            order = self.orders.get(order_id)
            if order is not None:
                if (order["sku"], order["quantity"]) != (sku, quantity):
                    return failure("ORDER_CONFLICT")
                return success(order)
            stock = self.skus.get(sku)
            if stock is None:
                return failure("SKU_NOT_FOUND")
            if stock["available"] < quantity:
                return failure("INSUFFICIENT_STOCK")
            order = {"order_id": order_id, "sku": sku, "quantity": quantity, "state": "RESERVED"}
            self.orders[order_id] = order
            stock["available"] -= quantity
            stock["reserved"] += quantity
            return success(order)

    def confirm(self, order_id):
        return self.settle(order_id, "CONFIRMED", "sold")

    def cancel(self, order_id):
        return self.settle(order_id, "CANCELLED", "available")

    def settle(self, order_id, target, destination):
        if not valid_id(order_id):
            return failure("INVALID_ARGUMENT")
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return failure("ORDER_NOT_FOUND")
            if order["state"] == target:
                return success(order)
            if order["state"] != "RESERVED":
                return failure("STATE_CONFLICT")
            stock = self.skus[order["sku"]]
            stock["reserved"] -= order["quantity"]
            stock[destination] += order["quantity"]
            order["state"] = target
            return success(order)

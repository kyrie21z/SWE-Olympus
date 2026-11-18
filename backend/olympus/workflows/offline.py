"""Version-bound, local fixtures for requirements shipped with the project."""

from pathlib import Path

from olympus.agents.runtime import AgentRunner, MockProvider
from olympus.api.errors import ApiError

ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "fixtures/requirements/inventory-reservation"
INVENTORY_BINDING = (
    "REQ-INVENTORY-RESERVATION", 1, "59c025f1a5c736265bb7a7e4a93ea4cb10d1a4b4dbf19c5add47ffb90fa2c1a3",
)
DOCUMENTS_BINDING = (
    "REQ-example", 1, "5d66b1caf3079d1524a59a1fbcbcc2307f56527ef1a4279c14bfb5a2f61d8501",
)


class OfflineRequirementUnsupported(ApiError):
    status_code = 409
    code = "OFFLINE_REQUIREMENT_UNSUPPORTED"


def task(number, kind, goal, sources, scope, predecessor=None):
    task_id = f"T{number:02}"
    dependency = f"T{predecessor:02}" if predecessor else None
    return {
        "task_id": task_id, "type": kind, "goal": goal, "sources": sources,
        "inputs": [f"artifact:{dependency}"] if dependency else [],
        "outputs": [f"artifact:{task_id}"], "depends_on": [dependency] if dependency else [],
        "executor": "Momus" if kind == "test" else "Hephaestus", "reviewer": "Argus",
        "write_scope": [scope], "checks": [f"check:{task_id}"], "resources": [],
    }


def element(identifier, sources, description, acceptance):
    return {"id": identifier, "sources": sources, "description": description, "acceptance_method": acceptance}


def inventory_profile(req):
    stock = ["FR-001", "FR-007", "FR-008", "FR-010", "FR-011", "NFR-001"]
    orders = ["FR-002", "FR-003", "FR-004", "FR-005", "FR-006", "FR-009", "NFR-002"]
    all_ids = [item["id"] for item in req.content["requirements"]]
    methods = ("create_sku", "reserve", "confirm", "cancel", "get_sku", "get_order")
    design = {
        "modules": ["库存与快照", "订单预占与结算", "独立验收检查"],
        "data": ["SKU 的 initial、available、reserved、sold", "订单编号、商品编号、数量及状态"],
        "interfaces": [f"InventoryService.{method}" for method in methods],
        "states": ["RESERVED", "CONFIRMED", "CANCELLED"],
        "concurrency": "用实例锁保护库存检查、状态迁移及快照，保持各 SKU 库存守恒。",
        "exceptions": ["INVALID_ARGUMENT", "SKU_EXISTS", "SKU_NOT_FOUND", "INSUFFICIENT_STOCK",
                       "ORDER_NOT_FOUND", "ORDER_CONFLICT", "STATE_CONFLICT"],
        "interaction": "先创建商品，再预占、确认或取消订单，通过快照检查实际库存。",
        "decisions": ["相同参数的重复订单返回原状态。", "失败请求不占用订单编号。"],
        "elements": [
            element("D-STOCK", stock, "SKU 唯一、严格区分大小写，内部库存无法通过公开接口直接改写。",
                    "核对边界、成功返回结构、快照隔离和并发查询期间的库存守恒。"),
            element("D-ORDER", orders, "维护订单的原子迁移、幂等及库存守恒。",
                    "重复确认和重复取消均返回原订单且库存不变；未知订单返回 ORDER_NOT_FOUND。核对失败后复用编号与并发竞争。"),
        ],
    }
    tasks = [
        task(1, "interface", "登记订单状态与六个公开方法", ["NFR-001", "D-STOCK"], "inventory/contract.py"),
        task(2, "implement", "实现商品、输入校验和查询快照", [*stock, "D-STOCK"], "inventory/service.py", 1),
        task(3, "implement", "实现订单迁移与库存守恒", [*orders, "D-ORDER"], "inventory", 2),
        task(4, "test", "执行完整库存验收及并发检查", [*all_ids, "D-STOCK", "D-ORDER"], "reports", 3),
    ]
    source = (INVENTORY / "offline_inventory.py").read_text(encoding="utf-8")
    stock_source, mark, _ = source.partition("        def reserve(")
    if not mark:
        raise ValueError("offline inventory fixture is missing its order implementation")
    checks = [
        "from inventory.contract import METHODS, STATES\n"
        "assert len(METHODS) == 6 and 'reserve' in METHODS and STATES[0] == 'RESERVED'\n",
        "from inventory.service import InventoryService\ns = InventoryService()\n"
        "assert s.create_sku('A', 1)['data']['initial'] == 1\n"
        "assert s.create_sku('A', 2)['error']['code'] == 'SKU_EXISTS'\n"
        "assert s.get_sku('A')['data']['available'] == 1\n",
        "from inventory.service import InventoryService\ns = InventoryService()\ns.create_sku('A', 3)\n"
        "assert s.reserve('O', 'A', 2)['data']['state'] == 'RESERVED'\n"
        "assert s.confirm('O')['data']['state'] == 'CONFIRMED'\n"
        "assert s.get_sku('A')['data']['sold'] == 2\n",
        (INVENTORY / "offline_checks.py").read_text(encoding="utf-8"),
    ]
    changes = {
        "T01": [{"path": "inventory/contract.py",
                 "content": f"METHODS = {methods!r}\nSTATES = ('RESERVED', 'CONFIRMED', 'CANCELLED')\n"}],
        "T02": [{"path": "inventory/service.py", "content": stock_source.rstrip() + "\n"}],
        "T03": [
            {"path": "inventory/service.py", "content": source},
            {"path": "inventory/README.md", "content": "# 库存预占系统\n\n"
             "在 Python 3.12 及以上使用 `from inventory.service import InventoryService`。"
             "在工作台查看 T04 的逐项验收结果。若需单独复验，把计划中 T04 的冻结脚本保存为 "
             "`inventory_checks.py`，在包含 `inventory` 目录的工件根目录运行 `python inventory_checks.py`。\n"},
        ],
    }
    return design, tasks, checks, changes


def documents_profile():
    sources = ["FR-001", "FR-002"]
    design = {
        "modules": ["团队文档版本库"], "data": ["标题与有序版本内容"],
        "interfaces": ["DocumentStore.save", "DocumentStore.search", "DocumentStore.read_version"],
        "states": ["saved"], "concurrency": "同一实例顺序保存和读取版本。", "exceptions": ["未找到版本返回 None"],
        "interaction": "输入标题关键词查找文档，按版本号读取历史内容。",
        "decisions": ["按保存先后分配版本号。"],
        "elements": [element("D-DOC", sources, "标题检索和文档历史版本。", "检查匹配、空结果及两次修订")],
    }
    tasks = [
        task(1, "implement", "实现文档检索和历史版本", [*sources, "D-DOC"], "documents/service.py"),
        task(2, "test", "核对检索结果及独立读取历史版本", [*sources, "D-DOC"], "reports", 1),
    ]
    checks = [
        "from documents.service import DocumentStore\ns = DocumentStore()\n"
        "assert s.save('交接指南', '初稿') == 1\nassert s.search('指南') == ['交接指南']\n",
        "from documents.service import DocumentStore\ns = DocumentStore()\n"
        "assert s.save('交接指南', '初稿') == 1\nassert s.save('交接指南', '修订') == 2\n"
        "assert s.search('指南') == ['交接指南'] and s.search('无匹配') == []\n"
        "assert s.read_version('交接指南', 1) == '初稿'\n"
        "assert s.read_version('交接指南', 2) == '修订'\n",
    ]
    changes = {"T01": [{"path": "documents/service.py", "content": """class DocumentStore:
    def __init__(self):
        self.documents = {}

    def save(self, title, content):
        versions = self.documents.setdefault(title, [])
        versions.append(content)
        return len(versions)

    def search(self, keyword):
        return sorted(title for title in self.documents if keyword in title)

    def read_version(self, title, version):
        versions = self.documents.get(title, [])
        return versions[version - 1] if 1 <= version <= len(versions) else None
"""}]}
    return design, tasks, checks, changes


def _binding(requirement):
    return requirement.object_id, requirement.version, requirement.content_hash


def supports_offline(requirement):
    return _binding(requirement) in {INVENTORY_BINDING, DOCUMENTS_BINDING}


def make_offline_runner(requirement, *, ledger):
    binding = _binding(requirement)
    if binding == INVENTORY_BINDING:
        design, tasks, checks, changes = inventory_profile(requirement)
    elif binding == DOCUMENTS_BINDING:
        design, tasks, checks, changes = documents_profile()
    else:
        raise OfflineRequirementUnsupported("此版本没有离线样例。可导入库存预占或团队文档样例，或配置模型 API 后重试。")

    review = {"output": {"verdict": "passed", "findings": []}}
    responses = {
        "Athena:design": {"output": {"design": design}},
        "Momus:design_review": review,
        "Daedalus:plan": {"output": {"tasks": tasks}},
        "Momus:plan_review": review,
        "Momus:acceptance_definition": {"output": {"tasks": [
            {"task_id": row["task_id"], "checks": {row["checks"][0]: script}}
            for row, script in zip(tasks, checks, strict=True)
        ]}},
        "Argus:artifact_review": review,
        **{f"Hephaestus:{task_id}": {"output": {"changes": files}} for task_id, files in changes.items()},
    }
    return AgentRunner(MockProvider(fixtures=responses), ledger=ledger)

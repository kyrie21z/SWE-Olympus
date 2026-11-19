"""Exact project APIs are checked without guessing unknown runtime types."""

import pytest

from olympus.agents.runtime import OutputSchemaError
from olympus.workflows.api_contracts import validate_api_contracts, validate_implementation_contracts


CONTRACTS = {
    "modules/DocumentStoreModule.py": {
        "DocumentStoreModule": ["save_document", "get_document", "list_versions"], "DocumentStoreError": [],
    },
    "modules/SearchModule.py": {"SearchModule": ["search_documents"]},
}


def test_current_wrong_import_and_bad_calls_are_reported_together_with_full_canonical_names():
    source = '''from modules.DocumentStoreModule import DocumentStore
from modules.SearchModule import SearchModule
store = DocumentStore()
store.save('a')
search = SearchModule(store)
search.search('a')
store.get_versions('a')
'''
    with pytest.raises(OutputSchemaError) as error:
        validate_api_contracts(source, CONTRACTS)
    assert len(error.value.validation_hints) == 4
    assert "modules.DocumentStoreModule.DocumentStore" in error.value.hint
    assert "modules.DocumentStoreModule.DocumentStoreModule.save" in error.value.hint
    assert "modules.SearchModule.SearchModule.search" in error.value.hint
    assert "modules.DocumentStoreModule.DocumentStoreModule.get_versions" in error.value.hint
    assert "modules.DocumentStoreModule.DocumentStoreModule.save_document" in error.value.hint
    assert "modules.SearchModule.SearchModule.search_documents" in error.value.hint


def test_import_aliases_module_qualification_constructors_and_local_aliases_are_valid():
    validate_api_contracts('''from modules.DocumentStoreModule import DocumentStoreModule as Store, DocumentStoreError
import modules.SearchModule as search_module
import modules.DocumentStoreModule
store = Store()
alias = store
alias.save_document('a')
search_module.SearchModule(store).search_documents('a')
modules.DocumentStoreModule.DocumentStoreModule().list_versions('a')
raise DocumentStoreError('expected')
''', CONTRACTS)


@pytest.mark.parametrize("source", [
    "import modules.DocumentStoreModule as module\nmodule.BadExport()\n",
    "from modules import DocumentStoreModule as module\nmodule.DocumentStoreModule().save()\n",
    "from modules.DocumentStoreModule import DocumentStoreModule as Store\nStore().save()\n",
])
def test_known_module_exports_and_qualified_instance_calls_cannot_bypass_contract(source):
    with pytest.raises(OutputSchemaError):
        validate_api_contracts(source, CONTRACTS)


def test_helper_return_tuple_unpack_and_function_local_aliases_keep_types():
    source = '''from modules.DocumentStoreModule import DocumentStoreModule
from modules.SearchModule import SearchModule
def make_context():
    store = DocumentStoreModule()
    search = SearchModule(store)
    return store, search
def test_calls():
    store, search = make_context()
    other = store
    other.save()
    search.search()
test_calls()
'''
    with pytest.raises(OutputSchemaError) as error:
        validate_api_contracts(source, CONTRACTS)
    assert len(error.value.validation_hints) == 2
    assert "DocumentStoreModule.save" in error.value.hint and "SearchModule.search" in error.value.hint


def test_constructor_parameters_and_explicit_parameter_annotations_carry_known_receivers():
    source = '''from modules.DocumentStoreModule import DocumentStoreModule
def build(cls):
    return cls()
def consume(store: DocumentStoreModule):
    store.save()
build(DocumentStoreModule).get_versions()
'''
    with pytest.raises(OutputSchemaError) as error:
        validate_api_contracts(source, CONTRACTS)
    assert "DocumentStoreModule.save" in error.value.hint and "DocumentStoreModule.get_versions" in error.value.hint


def test_direct_setup_attribute_type_is_available_to_test_methods():
    source = '''import unittest
from modules.DocumentStoreModule import DocumentStoreModule
class Checks(unittest.TestCase):
    def setUp(self):
        self.store = DocumentStoreModule()
    def test_document(self):
        alias = self.store
        alias.save()
'''
    with pytest.raises(OutputSchemaError, match="API contract") as error:
        validate_api_contracts(source, CONTRACTS)
    assert "DocumentStoreModule.save" in error.value.hint


def test_unknown_types_builtin_results_metadata_loops_and_local_shadowing_are_not_guessed():
    validate_api_contracts('''from modules.DocumentStoreModule import DocumentStoreModule
def known():
    store = DocumentStoreModule()
    result = store.get_document('a')
    result.get('title')
def unrelated(store):
    store.save()
    data = {}
    data.items()
for entry in source_metadata['reports/check-report.json']['data']['checks']:
    entry.get('output')
store = DocumentStoreModule()
store = {}
store.items()
''', CONTRACTS)


def test_errors_are_aggregated_and_bounded():
    source = "from modules.DocumentStoreModule import DocumentStoreModule\nstore = DocumentStoreModule()\n"
    source += "\n".join(f"store.bad_method_{index}()" for index in range(20))
    with pytest.raises(OutputSchemaError) as error:
        validate_api_contracts(source, CONTRACTS)
    assert len(error.value.validation_hints) == 8


@pytest.mark.parametrize("flow", [
    "for item in values:\n    store = {}\n",
    "while pending:\n    store = {}\n",
    "if condition:\n    store = {}\n",
    "try:\n    store = {}\nexcept Exception:\n    store = {}\n",
    "with context:\n    store = {}\n",
])
def test_control_flow_reassignment_does_not_leave_a_stale_project_receiver_type(flow):
    source = "from modules.DocumentStoreModule import DocumentStoreModule\nstore = DocumentStoreModule()\n"
    validate_api_contracts(source + flow + "store.items()\n", CONTRACTS)


def test_empty_legacy_contracts_do_not_change_existing_validation():
    validate_api_contracts("legacy unknown syntax is checked elsewhere", {})
    validate_implementation_contracts("legacy unknown syntax is checked elsewhere", "service.py", {})


def test_implementation_requires_canonical_classes_and_methods_without_creating_aliases():
    source = "class DocumentStore:\n    def save(self):\n        pass\n"
    with pytest.raises(OutputSchemaError) as error:
        validate_implementation_contracts(source, "modules/DocumentStoreModule.py", CONTRACTS)
    assert "DocumentStoreModule" in error.value.hint and "DocumentStoreError" in error.value.hint
    assert "兼容别名" in error.value.hint


def test_implementation_checks_required_methods_and_allows_same_file_base_inheritance_and_extras():
    source = '''class Base:
    def save_document(self):
        pass
    def get_document(self):
        pass
class DocumentStoreModule(Base):
    def list_versions(self):
        pass
    def extra_helper(self):
        pass
class DocumentStoreError(Exception):
    pass
'''
    validate_implementation_contracts(source, "modules\\DocumentStoreModule.py", CONTRACTS)
    with pytest.raises(OutputSchemaError) as error:
        validate_implementation_contracts(source.replace("def list_versions", "def get_versions"),
                                          "modules/DocumentStoreModule.py", CONTRACTS)
    assert "DocumentStoreModule.list_versions" in error.value.hint


def test_implementation_contract_does_not_apply_to_unrelated_files():
    validate_implementation_contracts("arbitrary nonpython document", "README.md", CONTRACTS)

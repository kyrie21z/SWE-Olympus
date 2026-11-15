from typing import TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph

from olympus.app import app


@pytest.mark.scenario
def test_health_and_langgraph_without_model_key():
    assert TestClient(app).get("/health").json()["model_required"] is False

    class State(TypedDict):
        checked: bool

    graph = StateGraph(State)
    graph.add_node("check", lambda state: {"checked": True})
    graph.add_edge(START, "check")
    graph.add_edge("check", END)
    assert graph.compile().invoke({"checked": False}) == {"checked": True}

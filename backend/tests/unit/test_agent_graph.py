import pytest

from autumn_backend.agent.graph import GraphState, Nodes, build_graph

pytestmark = pytest.mark.unit


async def test_graph_reauthorizes_after_tool_and_stops_for_durable_wait() -> None:
    visits = []

    async def authorize(state: GraphState):
        visits.append("authorize")
        return {"route": "plan"}

    async def context(state: GraphState):
        visits.append("context")
        return {}

    async def plan(state: GraphState):
        visits.append("plan")
        return {"route": "tool" if state["step"] == 0 else "wait"}

    async def tool(state: GraphState):
        visits.append("tool")
        return {"step": state["step"] + 1}

    async def wait(state: GraphState):
        visits.append("wait")
        return {}

    async def reply(state: GraphState):
        raise AssertionError("此任务必须持久等待")

    graph = build_graph(Nodes(authorize, context, plan, tool, wait, reply))
    result = await graph.ainvoke(
        {"run_id": "server-run", "generation": 1, "step": 0, "route": "plan"},
        config={"recursion_limit": 20},
    )
    assert visits == [
        "authorize",
        "context",
        "plan",
        "tool",
        "authorize",
        "context",
        "plan",
        "wait",
    ]
    assert result == {"run_id": "server-run", "generation": 1, "step": 1, "route": "wait"}

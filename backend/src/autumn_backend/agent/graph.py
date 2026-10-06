"""图状态只有调度元数据；身份、正文与工具结果由单次运行的服务端上下文持有。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph


class GraphState(TypedDict):
    run_id: str
    generation: int
    step: int
    route: Literal["plan", "tool", "wait", "reply", "stop"]


Node = Callable[[GraphState], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class Nodes:
    authorize: Node
    context: Node
    plan: Node
    tool: Node
    wait: Node
    reply: Node


def build_graph(
    nodes: Nodes, *, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph[GraphState, Any, Any, Any]:
    graph = StateGraph(GraphState)
    for name in ("authorize", "context", "plan", "tool", "wait", "reply"):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "authorize")
    graph.add_conditional_edges(
        "authorize",
        lambda state: "stop" if state["route"] == "stop" else "context",
        {"stop": END, "context": "context"},
    )
    graph.add_edge("context", "plan")
    graph.add_conditional_edges(
        "plan",
        lambda state: state["route"],
        {"tool": "tool", "wait": "wait", "reply": "reply", "stop": END},
    )
    # 每次循环重建当前身份与获准上下文；不能从旧节点直接跳入工具。
    graph.add_edge("tool", "authorize")
    graph.add_edge("wait", END)
    graph.add_edge("reply", END)
    return graph.compile(checkpointer=checkpointer)

"""StateGraph wiring + checkpointer plumbing.

Topology (single-node-per-invocation):

    START -> router --(extra['_next'])--> one stage node -> END

Most stage nodes END after one turn (await user input / external wait). A few
CHAIN within one invocation via a conditional edge that reads the same
`extra['_next']` the router uses:

    style   -> {anchor, songwriter, end}
    anchor  -> {songwriter, end}
    songwriter -> generate            (unconditional)
    choice  -> {pix, end}
    discovery_story -> {songwriter, end}   (lyrics regen)

`build_graph(checkpointer)` compiles the graph with whatever checkpointer it is
given — an AsyncPostgresSaver in production (runner), a MemorySaver in tests.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.anchor import anchor
from app.graph.nodes.choice import choice
from app.graph.nodes.deliver import deliver
from app.graph.nodes.discovery import discovery_recipient, discovery_story
from app.graph.nodes.followup import followup
from app.graph.nodes.generate import generate
from app.graph.nodes.pix import pix
from app.graph.nodes.preview import preview
from app.graph.nodes.songwriter_node import songwriter_node
from app.graph.nodes.style import style
from app.graph.nodes.welcome import welcome
from app.graph.router import router
from app.graph.state import ConversationState

# Every node the router may dispatch to (plus "end").
_ALL_TARGETS = {
    "welcome": "welcome",
    "discovery_recipient": "discovery_recipient",
    "discovery_story": "discovery_story",
    "style": "style",
    "anchor": "anchor",
    "songwriter": "songwriter",
    "generate": "generate",
    "preview": "preview",
    "choice": "choice",
    "pix": "pix",
    "deliver": "deliver",
    "followup": "followup",
    "end": END,
}


def _pick(state: dict) -> str:
    return (state.get("extra") or {}).get("_next") or "end"


def build_graph(checkpointer=None):
    """Wire and compile the Marina state machine."""
    g = StateGraph(ConversationState)

    g.add_node("router", router)
    g.add_node("welcome", welcome)
    g.add_node("discovery_recipient", discovery_recipient)
    g.add_node("discovery_story", discovery_story)
    g.add_node("style", style)
    g.add_node("anchor", anchor)
    g.add_node("songwriter", songwriter_node)
    g.add_node("generate", generate)
    g.add_node("preview", preview)
    g.add_node("choice", choice)
    g.add_node("pix", pix)
    g.add_node("deliver", deliver)
    g.add_node("followup", followup)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _pick, _ALL_TARGETS)

    # Terminal-after-one-turn nodes.
    g.add_edge("welcome", END)
    g.add_edge("discovery_recipient", END)
    g.add_edge("generate", END)
    g.add_edge("preview", END)
    g.add_edge("pix", END)
    g.add_edge("deliver", END)
    g.add_edge("followup", END)

    # Chaining nodes (read the same _next decision).
    g.add_conditional_edges("discovery_story", _pick, {"songwriter": "songwriter", "end": END})
    g.add_conditional_edges("style", _pick, {"anchor": "anchor", "songwriter": "songwriter", "end": END})
    g.add_conditional_edges("anchor", _pick, {"songwriter": "songwriter", "end": END})
    g.add_conditional_edges("choice", _pick, {"pix": "pix", "end": END})
    g.add_edge("songwriter", "generate")

    return g.compile(checkpointer=checkpointer)

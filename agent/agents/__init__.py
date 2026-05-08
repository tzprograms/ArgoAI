# ArgoCD Diagnostic Agents
#
# Each specialist agent is defined with full ADK metadata for A2A compatibility.
# The Router agent discovers agents via their AgentCards and routes intelligently.

from agent.agents.runtime_agent import runtime_agent, RUNTIME_AGENT_CARD
from agent.agents.config_agent import config_agent, CONFIG_AGENT_CARD
from agent.agents.network_agent import network_agent, NETWORK_AGENT_CARD
from agent.agents.storage_agent import storage_agent, STORAGE_AGENT_CARD
from agent.agents.rbac_agent import rbac_agent, RBAC_AGENT_CARD

# Registry of all specialist agents and their cards
SPECIALIST_AGENTS = {
    "runtime": {
        "agent": runtime_agent,
        "card": RUNTIME_AGENT_CARD,
    },
    "config": {
        "agent": config_agent,
        "card": CONFIG_AGENT_CARD,
    },
    "network": {
        "agent": network_agent,
        "card": NETWORK_AGENT_CARD,
    },
    "storage": {
        "agent": storage_agent,
        "card": STORAGE_AGENT_CARD,
    },
    "rbac": {
        "agent": rbac_agent,
        "card": RBAC_AGENT_CARD,
    },
}

__all__ = [
    "SPECIALIST_AGENTS",
    "runtime_agent", "RUNTIME_AGENT_CARD",
    "config_agent", "CONFIG_AGENT_CARD",
    "network_agent", "NETWORK_AGENT_CARD",
    "storage_agent", "STORAGE_AGENT_CARD",
    "rbac_agent", "RBAC_AGENT_CARD",
]

# Legacy prompts - kept for backward compatibility
#
# NEW: Use agent-specific prompts from:
# - runtime_agent.py
# - config_agent.py
# - network_agent.py
# - storage_agent.py
# - rbac_agent.py

# Re-export new prompts for any legacy code
from agent.agents.runtime_agent import RUNTIME_AGENT_PROMPT
from agent.agents.config_agent import CONFIG_AGENT_PROMPT
from agent.agents.network_agent import NETWORK_AGENT_PROMPT
from agent.agents.storage_agent import STORAGE_AGENT_PROMPT
from agent.agents.rbac_agent import RBAC_AGENT_PROMPT

# Legacy aliases
LOG_ANALYZER_PROMPT = RUNTIME_AGENT_PROMPT
HYPOTHESIS_VALIDATOR_PROMPT = """Create final diagnosis JSON:
{{"rootCause": "exact error", "confidence": "high", "suggestedFix": "specific fix"}}"""

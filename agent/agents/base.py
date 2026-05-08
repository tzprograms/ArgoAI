# Base classes and utilities for specialist agents
#
# Uses ADK's agent metadata patterns for A2A compatibility.

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AgentSkill:
    """Represents a skill/capability that an agent has.
    
    This follows the A2A protocol's skill specification.
    """
    id: str
    name: str
    description: str
    tags: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)


@dataclass 
class AgentCard:
    """Agent Card following A2A protocol specification.
    
    This is a simplified version of the full A2A AgentCard that contains
    the metadata needed for intelligent routing decisions.
    
    See: https://a2a-protocol.org/dev/topics/agent-discovery
    """
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    skills: List[AgentSkill] = field(default_factory=list)
    
    # Keywords that indicate this agent should handle the issue
    # Used for fast heuristic matching before LLM routing
    trigger_keywords: List[str] = field(default_factory=list)
    trigger_event_reasons: List[str] = field(default_factory=list)
    
    # Conditions under which this agent is most appropriate
    health_conditions: List[str] = field(default_factory=list)
    sync_conditions: List[str] = field(default_factory=list)
    
    def matches_heuristic(self, signals: dict) -> tuple[bool, str]:
        """Check if this agent matches based on fast heuristics.
        
        Checks (in priority order):
        1. Pod state reasons (most reliable -- directly from container status)
        2. Event reasons
        3. Keywords in event messages
        4. Health/sync conditions
        """
        health = signals.get('health_status', signals.get('healthStatus', ''))
        sync = signals.get('sync_status', signals.get('syncStatus', ''))
        warnings = signals.get('events', signals.get('warningEvents')) or []

        # Check pod state reasons first (most specific signal)
        for ps in (signals.get('podStatuses') or []):
            for key in ('stateReason', 'lastTerminatedReason'):
                reason = ps.get(key, '')
                if reason and reason in self.trigger_event_reasons:
                    return True, f"Pod state '{reason}' matches agent"

        # Check event reasons
        for w in warnings:
            reason = w.get('reason', '')
            if reason in self.trigger_event_reasons:
                return True, f"Event reason '{reason}' matches agent"

        # Check keywords in event messages and pod state reasons
        all_text_parts = [
            w.get('reason', '') + ' ' + w.get('message', '')
            for w in warnings
        ]
        for ps in (signals.get('podStatuses') or []):
            for key in ('stateReason', 'lastTerminatedReason'):
                if ps.get(key):
                    all_text_parts.append(ps[key])
        all_text = ' '.join(all_text_parts).lower()

        for kw in self.trigger_keywords:
            if kw.lower() in all_text:
                return True, f"Keyword '{kw}' found in signals"

        # Check health/sync conditions
        if health in self.health_conditions:
            return True, f"Health status '{health}' matches"
        if sync in self.sync_conditions:
            return True, f"Sync status '{sync}' matches"

        return False, ""
    
    def to_prompt_description(self) -> str:
        """Generate a description suitable for LLM routing prompts."""
        skills_text = "\n".join([
            f"  - {s.name}: {s.description}"
            for s in self.skills
        ])
        examples_text = ", ".join(self.skills[0].examples[:5]) if self.skills and self.skills[0].examples else "N/A"
        
        return f"""- {self.id}: {self.name}
  Description: {self.description}
  Skills:
{skills_text}
  Example issues: {examples_text}"""

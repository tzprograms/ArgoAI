# Router Agent - Decisive Heuristic Routing using Agent Cards
#
# Discovers specialist agents via their AgentCards and routes
# diagnosis requests to the most appropriate agent.
#
# QUOTA-SURGICAL Routing Strategy (NO LLM triage):
# 1. Fast heuristic matching using AgentCard.matches_heuristic()
# 2. Priority-based selection when multiple agents match
# 3. Default to runtime agent if no matches (never call LLM for routing)
# 4. Cache routing decisions with 20-minute TTL

import hashlib
import logging
import time
from typing import Optional, List

from agent.agents.base import AgentCard
from agent import metrics as prom_metrics

logger = logging.getLogger(__name__)

# Priority order for decisive routing when multiple agents match
# Lower index = higher priority (more specific agents first)
AGENT_PRIORITY = ["storage", "rbac", "network", "config", "runtime"]


class AgentCardRouter:
    """Routes to specialist agents using their Agent Cards.
    
    This implements A2A-style discovery where each agent exposes
    metadata about its capabilities via an AgentCard.
    
    QUOTA-SURGICAL: Eliminates LLM triage calls by using decisive
    priority-based selection when multiple agents match.
    """
    
    def __init__(self, agent_cards: List[AgentCard]):
        """Initialize router with available agent cards.
        
        Args:
            agent_cards: List of AgentCards from specialist agents
        """
        self.agent_cards = {card.id: card for card in agent_cards}
        self._route_cache: dict[str, tuple[str, str, float]] = {}
        self._cache_ttl = 1200  # 20 minutes (increased from 10)
        
        logger.info(f"Router initialized with {len(agent_cards)} agents: {list(self.agent_cards.keys())}")
    
    def _fingerprint(self, signals: dict) -> str:
        """Create fingerprint for route caching.
        
        QUOTA-SURGICAL: Includes health, sync, warning reasons, and pod stateReasons
        for precise cache matching while excluding transient data (pod names, timestamps).
        """
        health = signals.get('healthStatus', signals.get('health_status', ''))
        sync = signals.get('syncStatus', signals.get('sync_status', ''))
        warnings = signals.get('warningEvents', signals.get('events')) or []
        
        # Use first 5 warning reasons
        reasons = sorted([w.get('reason', '') for w in warnings[:5] if w.get('reason')])
        
        # Include container state reasons from pre-loaded pod statuses
        pod_statuses = signals.get('podStatuses') or []
        state_reasons = set()
        for ps in pod_statuses[:5]:
            if ps.get('stateReason'):
                state_reasons.add(ps.get('stateReason'))
            if ps.get('lastTerminatedReason'):
                state_reasons.add(ps.get('lastTerminatedReason'))
        
        key = f"{health}|{sync}|{'|'.join(reasons)}|{'|'.join(sorted(state_reasons))}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]
    
    def _get_cached_route(self, signals: dict) -> Optional[tuple[str, str]]:
        """Check cache for existing routing decision."""
        fp = self._fingerprint(signals)
        if fp in self._route_cache:
            agent_id, reason, timestamp = self._route_cache[fp]
            if time.time() - timestamp < self._cache_ttl:
                logger.info(f"Route cache hit: {agent_id}")
                return agent_id, reason
            del self._route_cache[fp]
        return None
    
    def _cache_route(self, signals: dict, agent_id: str, reason: str):
        """Cache a routing decision."""
        fp = self._fingerprint(signals)
        self._route_cache[fp] = (agent_id, reason, time.time())
        
        # Evict old entries (keep max 150 entries)
        if len(self._route_cache) > 150:
            oldest = min(self._route_cache.keys(),
                        key=lambda k: self._route_cache[k][2])
            del self._route_cache[oldest]
    
    def _select_by_priority(self, matches: List[tuple[str, str]]) -> tuple[str, str]:
        """Select agent by priority when multiple match.
        
        Priority order: storage > rbac > network > config > runtime
        This ensures more specific issues (storage, RBAC) are caught
        before general-purpose agents handle them.
        """
        match_dict = {agent_id: reason for agent_id, reason in matches}
        
        for agent_id in AGENT_PRIORITY:
            if agent_id in match_dict:
                return agent_id, match_dict[agent_id]
        
        # Fallback to first match if priority list doesn't cover
        return matches[0]
    
    def route_heuristic(self, signals: dict) -> tuple[str, str, bool]:
        """Fast routing using AgentCard heuristics.
        
        QUOTA-SURGICAL: Always returns is_confident=True to prevent
        LLM fallback. Uses priority-based selection for multiple matches.
        
        Returns:
            tuple[str, str, bool]: (agent_id, reason, is_confident)
            Note: is_confident is ALWAYS True now (no LLM triage)
        """
        start_time = time.time()
        
        # Check cache first
        cached = self._get_cached_route(signals)
        if cached:
            prom_metrics.TRIAGE_DECISIONS.labels(agent=cached[0], method="cached").inc()
            prom_metrics.TRIAGE_DURATION.labels(method="cached").observe(time.time() - start_time)
            return cached[0], cached[1], True
        
        # Try each agent's heuristic matching
        matches = []
        pod_state_matches = []
        for agent_id, card in self.agent_cards.items():
            matched, reason = card.matches_heuristic(signals)
            if matched:
                matches.append((agent_id, reason))
                if "Pod state" in reason:
                    pod_state_matches.append((agent_id, reason))

        # Pod-state matches are the most reliable (scoped to the app's pods)
        # Prefer them over event-based matches which may include namespace noise
        if pod_state_matches:
            if len(pod_state_matches) == 1:
                agent_id, reason = pod_state_matches[0]
            else:
                agent_id, reason = self._select_by_priority(pod_state_matches)
            self._cache_route(signals, agent_id, reason)
            prom_metrics.TRIAGE_DECISIONS.labels(agent=agent_id, method="pod_state").inc()
            prom_metrics.TRIAGE_DURATION.labels(method="pod_state").observe(time.time() - start_time)
            return agent_id, reason, True

        # If exactly one match, we're confident
        if len(matches) == 1:
            agent_id, reason = matches[0]
            self._cache_route(signals, agent_id, reason)
            prom_metrics.TRIAGE_DECISIONS.labels(agent=agent_id, method="heuristic").inc()
            prom_metrics.TRIAGE_DURATION.labels(method="heuristic").observe(time.time() - start_time)
            return agent_id, reason, True

        # Multiple matches from events - use priority selection
        if len(matches) > 1:
            agent_id, reason = self._select_by_priority(matches)
            logger.info(f"Multiple heuristic matches: {[m[0] for m in matches]}, selected {agent_id} by priority")
            self._cache_route(signals, agent_id, reason)
            prom_metrics.TRIAGE_DECISIONS.labels(agent=agent_id, method="heuristic_priority").inc()
            prom_metrics.TRIAGE_DURATION.labels(method="heuristic_priority").observe(time.time() - start_time)
            return agent_id, f"[priority] {reason}", True
        
        # No matches - default to runtime (never call LLM)
        agent_id = "runtime"
        reason = "No heuristic match - defaulting to runtime analyzer"
        self._cache_route(signals, agent_id, reason)
        prom_metrics.TRIAGE_DECISIONS.labels(agent=agent_id, method="default").inc()
        prom_metrics.TRIAGE_DURATION.labels(method="default").observe(time.time() - start_time)
        logger.info(f"No heuristic matches, defaulting to runtime")
        return agent_id, reason, True  # Always confident (no LLM triage)
    
    async def route(
        self,
        signals: dict,
        provider: str,
        api_key: str,
        model_name: str = ""
    ) -> tuple[str, str]:
        """Main routing method - DECISIVE heuristic-only strategy.
        
        QUOTA-SURGICAL: Completely eliminates LLM triage calls.
        Strategy:
        1. Try fast heuristic matching
        2. Use priority-based selection for multiple matches
        3. Default to runtime if no matches
        4. NEVER call LLM for routing decisions
        
        Returns:
            tuple[str, str]: (agent_id, reason)
        """
        # Always use heuristics - no LLM triage
        agent_id, reason, _ = self.route_heuristic(signals)
        return agent_id, reason
    
    def get_agent_card(self, agent_id: str) -> Optional[AgentCard]:
        """Get agent card by ID."""
        return self.agent_cards.get(agent_id)
    
    def list_agents(self) -> List[dict]:
        """List all available agents with their metadata."""
        return [
            {
                "id": card.id,
                "name": card.name,
                "description": card.description,
                "skills": [s.name for s in card.skills],
            }
            for card in self.agent_cards.values()
        ]

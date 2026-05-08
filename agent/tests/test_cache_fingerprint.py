"""Tests for semantic cache fingerprinting.

Verifies that cache fingerprints correctly differentiate:
- Different image names for ImagePullBackOff
- Different configmaps for CreateContainerConfigError
- Different PVCs for storage errors
"""

import pytest
from agent.engine import SemanticCache


class TestCacheFingerprinting:
    """Test suite for cache fingerprinting."""

    def test_different_images_different_fingerprints(self):
        """Two ImagePullBackOff errors with different images should not share cache."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals1 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "ErrImagePull",
                    "message": 'Failed to pull image "nginx:bad-tag-1"',
                    "type": "Warning"
                }
            ]
        }
        
        signals2 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "ErrImagePull",
                    "message": 'Failed to pull image "redis:nonexistent"',
                    "type": "Warning"
                }
            ]
        }
        
        fp1 = cache._fingerprint(signals1)
        fp2 = cache._fingerprint(signals2)
        
        assert fp1 != fp2, "Different images should produce different fingerprints"

    def test_same_image_same_fingerprint(self):
        """Same image error should produce same fingerprint."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals1 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "appName": "app-1",  # Different app names
            "warningEvents": [
                {
                    "reason": "ErrImagePull",
                    "message": 'Failed to pull image "nginx:bad-tag"',
                    "type": "Warning"
                }
            ]
        }
        
        signals2 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "appName": "app-2",  # Different app names
            "warningEvents": [
                {
                    "reason": "ErrImagePull",
                    "message": 'Failed to pull image "nginx:bad-tag"',
                    "type": "Warning"
                }
            ]
        }
        
        fp1 = cache._fingerprint(signals1)
        fp2 = cache._fingerprint(signals2)
        
        assert fp1 == fp2, "Same image error should produce same fingerprint"

    def test_different_configmaps_different_fingerprints(self):
        """Different configmap errors should not share cache."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals1 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "CreateContainerConfigError",
                    "message": 'configmap "config-a" not found',
                    "type": "Warning"
                }
            ]
        }
        
        signals2 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "CreateContainerConfigError",
                    "message": 'configmap "config-b" not found',
                    "type": "Warning"
                }
            ]
        }
        
        fp1 = cache._fingerprint(signals1)
        fp2 = cache._fingerprint(signals2)
        
        assert fp1 != fp2, "Different configmaps should produce different fingerprints"

    def test_different_pvcs_different_fingerprints(self):
        """Different PVC errors should not share cache."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals1 = {
            "healthStatus": "Progressing",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "FailedScheduling",
                    "message": 'persistentvolumeclaim "pvc-data" not found',
                    "type": "Warning"
                }
            ]
        }
        
        signals2 = {
            "healthStatus": "Progressing",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "FailedScheduling",
                    "message": 'persistentvolumeclaim "pvc-logs" not found',
                    "type": "Warning"
                }
            ]
        }
        
        fp1 = cache._fingerprint(signals1)
        fp2 = cache._fingerprint(signals2)
        
        assert fp1 != fp2, "Different PVCs should produce different fingerprints"

    def test_cache_set_and_get(self):
        """Test cache set and get operations."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {
                    "reason": "OOMKilled",
                    "message": "Container killed due to OOM",
                    "type": "Warning"
                }
            ]
        }
        
        diagnosis = {
            "error": "OOMKilled",
            "cause": "Memory limit exceeded",
            "fix": "Increase memory limits"
        }
        
        # Set and get
        cache.set(signals, diagnosis)
        result = cache.get(signals)
        
        assert result == diagnosis, "Cache should return stored diagnosis"

    def test_cache_miss_for_different_signals(self):
        """Cache should miss for different signals."""
        cache = SemanticCache(ttl_seconds=300)
        
        signals1 = {
            "healthStatus": "Degraded",
            "syncStatus": "Synced",
            "warningEvents": [
                {"reason": "OOMKilled", "message": "OOM", "type": "Warning"}
            ]
        }
        
        signals2 = {
            "healthStatus": "Degraded",
            "syncStatus": "OutOfSync",  # Different sync status
            "warningEvents": [
                {"reason": "OOMKilled", "message": "OOM", "type": "Warning"}
            ]
        }
        
        cache.set(signals1, {"error": "test"})
        result = cache.get(signals2)
        
        assert result is None, "Different signals should not hit cache"

    def test_extract_cause_details_image(self):
        """Test image extraction from warning messages."""
        cache = SemanticCache(ttl_seconds=300)
        
        warnings = [
            {
                "reason": "ErrImagePull",
                "message": 'Failed to pull image "myregistry.io/app:v1.2.3"'
            }
        ]
        
        details = cache._extract_cause_details(warnings)
        
        assert "image:myregistry.io/app:v1.2.3" in details

    def test_extract_cause_details_configmap(self):
        """Test configmap extraction from warning messages."""
        cache = SemanticCache(ttl_seconds=300)
        
        warnings = [
            {
                "reason": "CreateContainerConfigError",
                "message": 'configmap "my-app-config" not found'
            }
        ]
        
        details = cache._extract_cause_details(warnings)
        
        assert "configmap:my-app-config" in details


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

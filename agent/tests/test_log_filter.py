"""Tests for log filtering.

Verifies that:
- Error lines are extracted
- Warning lines are included
- INFO noise is filtered out
- Stack traces are captured
- Output is bounded
"""

import pytest
from agent.tools.k8s_tools import _LogFilter


class TestLogFilter:
    """Test suite for log filtering."""

    def setup_method(self):
        """Create filter instance for each test."""
        self.filter = _LogFilter(max_output_chars=2000)

    def test_extracts_error_lines(self):
        """Filter should extract ERROR level lines."""
        logs = """
2024-01-15 10:00:00 INFO Starting application
2024-01-15 10:00:01 INFO Connected to database
2024-01-15 10:00:02 ERROR Connection refused to redis:6379
2024-01-15 10:00:03 INFO Retrying connection
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "ERRORS" in result
        assert "Connection refused" in result
        assert "Starting application" not in result

    def test_extracts_panic_fatal(self):
        """Filter should extract PANIC and FATAL lines."""
        logs = """
INFO: Normal operation
FATAL: Out of memory
PANIC: Stack overflow detected
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "FATAL" in result or "Out of memory" in result
        assert "PANIC" in result or "Stack overflow" in result

    def test_extracts_warnings(self):
        """Filter should include WARNING lines."""
        logs = """
INFO: Starting
WARNING: Deprecated API used
INFO: Processing
WARN: High memory usage
        """
        
        result = self.filter.filter_logs(logs)
        
        # Warnings may be included if space allows
        assert "Deprecated" in result or "High memory" in result or "WARNINGS" in result

    def test_extracts_exit_codes(self):
        """Filter should capture non-zero exit codes."""
        logs = """
Starting process
Process exited with code 1
Cleanup complete
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "exit" in result.lower() or "code 1" in result

    def test_extracts_permission_errors(self):
        """Filter should capture permission-related errors."""
        logs = """
INFO: Checking permissions
ERROR: Access denied to /var/data
INFO: Retrying with elevated privileges
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "denied" in result.lower()

    def test_captures_stack_trace_context(self):
        """Filter should capture lines after ERROR as stack trace context."""
        logs = """
INFO: Processing request
ERROR: NullPointerException in handler
    at com.app.Handler.process(Handler.java:42)
    at com.app.Main.run(Main.java:15)
INFO: Request completed
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "NullPointerException" in result
        # Should include at least some stack trace context
        assert "Handler" in result or "process" in result

    def test_handles_empty_logs(self):
        """Filter should handle empty input gracefully."""
        result = self.filter.filter_logs("")
        
        assert "No logs available" in result

    def test_handles_none_logs(self):
        """Filter should handle None input."""
        result = self.filter.filter_logs(None)
        
        assert "No logs available" in result

    def test_deduplicates_repeated_errors(self):
        """Filter should deduplicate repeated error lines."""
        logs = """
2024-01-15 10:00:00 ERROR Connection refused
2024-01-15 10:00:01 ERROR Connection refused
2024-01-15 10:00:02 ERROR Connection refused
2024-01-15 10:00:03 ERROR Connection refused
        """
        
        result = self.filter.filter_logs(logs)
        
        # Should not have 4 copies of the same error
        count = result.count("Connection refused")
        assert count < 4

    def test_respects_max_output_chars(self):
        """Filter should respect output size limit."""
        # Generate a very long log
        logs = "\n".join([f"ERROR: Error message number {i} with extra text" for i in range(100)])
        
        result = self.filter.filter_logs(logs)
        
        assert len(result) <= self.filter.max_output

    def test_fallback_to_last_lines(self):
        """If no explicit errors, return last N lines."""
        logs = """
INFO: Step 1 complete
INFO: Step 2 complete
INFO: Step 3 complete
DEBUG: Internal state ok
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "No explicit errors found" in result or "Last" in result

    def test_extracts_oom_killed(self):
        """Filter should capture OOMKilled indicators."""
        logs = """
INFO: Starting container
container killed due to OOM
INFO: Container restarting
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "killed" in result.lower() or "oom" in result.lower()

    def test_extracts_crashloop(self):
        """Filter should capture CrashLoopBackOff indicators."""
        logs = """
INFO: Starting
ERROR: Crashloop detected for container main
Back-off restarting failed container
        """
        
        result = self.filter.filter_logs(logs)
        
        assert "crashloop" in result.lower() or "back-off" in result.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

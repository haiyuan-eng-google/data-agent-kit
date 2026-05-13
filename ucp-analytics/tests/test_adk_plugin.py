"""Direct unit coverage for the ADK plugin's UCP-tool detection.

The plugin's `_is_ucp_tool` uses substring matching against
`_UCP_PATTERNS`, so patterns that are too broad will silently
record unrelated agent tools (e.g. weather, simulation, custom
domain logic) as UCP traffic — corrupting the analytics dataset
for operators who don't override `track_all_tools`.

The `google.adk` package is an optional extra; the plugin module
defers to a stub `object` base class when ADK isn't installed, but
the `_UCP_PATTERNS` attribute is always available so these
classification-shape tests don't require the extra.
"""

from __future__ import annotations

from ucp_analytics.adk_plugin import UCPAgentAnalyticsPlugin


def _is_ucp_tool(name: str) -> bool:
    """Mirror the plugin's `_is_ucp_tool` substring check.

    Kept inline rather than instantiating the plugin (which would
    require BigQuery credentials at construction time) so the test
    runs in dev environments without GCP.
    """
    lower = name.lower()
    return any(p in lower for p in UCPAgentAnalyticsPlugin._UCP_PATTERNS)


class TestUcpToolPatternDetection:
    """Pin which agent tool names count as UCP traffic. Substring
    matching is intentionally cheap, but each pattern needs to be
    specific enough that unrelated agent tools (weather, search,
    custom domains) don't get pulled in."""

    def test_simulate_shipping_matches(self):
        # The UCP samples server's `/testing/simulate-shipping/{id}`
        # endpoint maps to this tool name.
        assert _is_ucp_tool("simulate_shipping")

    def test_simulate_weather_does_not_match(self):
        # Was an over-match when the pattern was the bare "simulate";
        # an agent that also calls `simulate_weather` would have its
        # rows recorded as UCP request events.
        assert not _is_ucp_tool("simulate_weather")

    def test_weather_simulation_does_not_match(self):
        # Same broad-substring concern in the other word order.
        assert not _is_ucp_tool("weather_simulation")

    def test_create_checkout_matches(self):
        assert _is_ucp_tool("create_checkout")

    def test_discover_merchant_matches(self):
        assert _is_ucp_tool("discover_merchant")

    def test_get_weather_does_not_match(self):
        # Sanity baseline — a typical non-UCP agent tool.
        assert not _is_ucp_tool("get_weather")

    def test_unrelated_database_tool_does_not_match(self):
        # Pin that the patterns don't accidentally catch
        # `database_lookup` via the `catalog_lookup` pattern's
        # `lookup` substring (which IS in `_TOOL_TO_HTTP` but
        # is matched as `catalog_lookup`, not bare `lookup`).
        assert not _is_ucp_tool("database_lookup")
        assert not _is_ucp_tool("file_lookup")

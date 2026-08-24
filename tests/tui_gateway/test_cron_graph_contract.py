"""Gateway execution contract for the health-probing cron graph."""

from tui_gateway import server


def test_cron_graph_is_pool_routed():
    assert "cron.graph" in server._LONG_HANDLERS

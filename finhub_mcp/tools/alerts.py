# Copyright (c) 2026 bachbnt. All rights reserved.

from finhub_mcp import alerts
from finhub_mcp.responses import error, invalid, success
from finhub_mcp.tooling import register_tool


def register(mcp) -> None:
    tool = register_tool(mcp)

    @tool
    def add_alert(
        symbol: str,
        condition: str,
        price: float,
        asset_type: str = 'crypto',
        telegram_chat_id: str = '',
    ) -> str:
        """Create a price alert that will be monitored by alert_daemon.py."""
        alert, validation_error = alerts.add_alert(symbol, condition, price, asset_type, telegram_chat_id)
        if validation_error:
            return invalid(validation_error)
        return success(alert)

    @tool
    def list_alerts() -> str:
        """List all price alerts currently stored in the alert store."""
        return success(alerts.load_alerts())

    @tool
    def remove_alert(alert_id: str) -> str:
        """Delete a price alert by its ID."""
        if not alerts.remove_alert(alert_id):
            return error(f"Alert ID not found: {alert_id}", error_type='not_found')
        return success({'removed': True, 'alert_id': alert_id})


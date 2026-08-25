"""Thin stdio MCP adapter for AI Video Station's documented REST API."""

from .client import AVSClient, AVSClientError, AVSSettings
from .tools import TOOL_SPECS, MCPToolBindings

__all__ = ["AVSClient", "AVSClientError", "AVSSettings", "MCPToolBindings", "TOOL_SPECS"]

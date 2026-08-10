from __future__ import annotations

import sys
import types

from avs_mcp_adapter.client import AVSClient, AVSSettings
from avs_mcp_adapter.server import create_server
from avs_mcp_adapter.tools import TOOL_SPECS


def test_server_registers_each_allow_listed_tool_with_mcp_annotations(monkeypatch) -> None:
    class FakeAnnotations:
        def __init__(self, **values):
            self.values = values

    class FakeServer:
        def __init__(self, *, name, title=None, lifespan=None):
            self.name = name
            self.title = title
            self.lifespan = lifespan
            self.registered = []

        def tool(self, **metadata):
            def register(function):
                self.registered.append((metadata, function))
                return function
            return register

    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    types_module = types.ModuleType("mcp.types")
    server_module.MCPServer = FakeServer
    types_module.ToolAnnotations = FakeAnnotations
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.server", server_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)

    client = AVSClient(AVSSettings("http://avs.test", "avs_agent_test"))
    server = create_server(client)
    assert server.name == "ai_video_station_mcp"
    assert server.title == "AI Video Station"
    assert {metadata["name"] for metadata, _ in server.registered} == {spec.name for spec in TOOL_SPECS}
    search = next(metadata for metadata, _ in server.registered if metadata["name"] == "avs_search_media")
    assert search["annotations"].values == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }

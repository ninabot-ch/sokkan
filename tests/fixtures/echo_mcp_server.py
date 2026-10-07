"""A one-tool stdio MCP server (runner integration test): proves that a session container
reaches the api's MCP servers through the relay, with the identity the api set."""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def whoami() -> str:
    """Who the api says this session is."""
    return os.environ.get("SOKKAN_SESSION_USER", "?")


if __name__ == "__main__":
    mcp.run()

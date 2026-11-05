"""The MCP server, called through the SDK's in-memory client. Skipped without the mcp extra."""
import os
import unittest

try:
    import anyio
    from mcp import Client
except ImportError:          # pip install ".[mcp]" to run these
    Client = None

COCKPIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo", "cockpit")


@unittest.skipIf(Client is None, "the mcp package is not installed")
class McpTest(unittest.TestCase):
    def call(self, name, args):
        from blueprint import mcp_server

        async def go():
            async with Client(mcp_server.build()) as c:
                tools = sorted(t.name for t in (await c.list_tools()).tools)
                return tools, await c.call_tool(name, args)
        return anyio.run(go)

    def test_tools_and_a_check(self):
        tools, r = self.call("check_trace", {"trace": os.path.join(COCKPIT, "traces", "deadlock.kev"),
                                             "contract": os.path.join(COCKPIT, "blueprint.yaml")})
        self.assertEqual(tools, ["check_policy", "check_trace", "draft_contract", "list_demo_traces", "write_policy"])
        self.assertFalse(r.is_error)
        result = r.structured_content["result"]
        self.assertEqual(result["result"], "fail")
        self.assertEqual(sorted({f["check"] for f in result["findings"] if f["severity"] == "error"}),
                         ["unexpected-edge", "wait-cycle"])

    def test_demo_traces_exist(self):
        _, r = self.call("list_demo_traces", {})
        demo = r.structured_content["result"]
        self.assertEqual(len(demo["traces"]), 15)
        self.assertTrue(all(os.path.exists(t["trace"]) for t in demo["traces"]))


if __name__ == "__main__":
    unittest.main()

# Backend environment variables (Railway > Variables)

| Variable | Zaroori? | Matlab |
|---|---|---|
| `MCP_SECRET` | haan (>=24 chars) | Claude connector URL ka hissa: `/mcp/<MCP_SECRET>` |
| `DEVICE_TOKEN` ya `DEVICES` | haan | phone ka relay token (`DEVICES=id:platform:token,...` kayi devices) |
| `ANTHROPIC_API_KEY` | optional | `agent_start` tools ke liye. **Bina iske agent band.** Har agent step par screen text Anthropic API ko jaata hai; bill aapko. |
| `AGENT_MODEL` | optional | default `claude-sonnet-5-5`; sasta/tez: `claude-haiku-4-5-20251001` |
| `LIVE_VIEW` | optional | `1` = browser live view `/live/<MCP_SECRET>` ON (default OFF, screenshots private hote hain) |

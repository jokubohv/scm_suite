# MCP gateway (API-key only)

The MCP server is exposed as **Streamable HTTP JSON-RPC 2.0** at:

```text
POST /api/v1/mcp
```

It is **API-key only**: `X-API-Key` is required and JWT bearer tokens are rejected.

## Discovery

```bash
curl -H "X-API-Key: $SCM_API_KEY" https://scm.k1hub.com/api/v1/mcp
```

The discovery response contains the protocol version, auth contract, tool count, tools with input
schemas, resources and prompts.

## Protocol methods

- `initialize` / `notifications/initialized`
- `ping`
- `tools/list` / `tools/call`
- `resources/list` / `resources/read`
- `prompts/list` / `prompts/get`

## Tool catalogue

The server derives a curated catalogue from the runtime OpenAPI document:

- CRUD tools for items, UOMs, locations, bins, inventory statuses, adjustments, transfers, counts,
  vendors, purchase orders, receipts, bills, warehouses, inbound shipments, receiving, putaway,
  waves, pick/put tasks, assemblies, BOMs, work orders, routings, work centers, quality,
  consignment, warranty, UPC registry, pricing, allocation runs and reports
- named lifecycle tools such as submit/post/approve/release/complete/capture/cancel
- `scm_api_request` fallback for any remaining `/api/v1/` business endpoint
- resources: `scm://openapi`, `scm://mcp/info`, `scm://mcp/tools`
- prompts: `inventory_adjustment`, `procure_to_pay`, `cycle_count`

Every tool input schema is generated from the runtime OpenAPI contract. All request bodies are now typed in the runtime API
contract (0 opaque body operations), so all 151 body-bearing MCP tools expose real fields, required
flags, enums and nested line schemas. Every tool inherits the caller's tenant and API-key scopes. Write calls automatically receive an
`Idempotency-Key`; approval tools require a key with the relevant approval scope.

## Examples

```bash
# tools/list
curl -s https://scm.k1hub.com/api/v1/mcp \
  -H "X-API-Key: $SCM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# call a tool
curl -s https://scm.k1hub.com/api/v1/mcp \
  -H "X-API-Key: $SCM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"items_list","arguments":{}}}'
```

## Security rules

- MCP path rejects JWT credentials
- tools cannot call auth/me/admin/platform/bootstrap/internal/MCP paths
- `scm_api_request` is limited to `/api/v1/` business paths
- resource/read responses never include secrets
- approval segregation is preserved by the underlying REST scopes

---
name: codemode-types
description: TypeScript definition generation from MCP Tool JSON Schema
---

# codemode-types

## When to use
Use when compiling MCP server tool definitions and JSON Schemas into compact, strongly-typed TypeScript definitions:
- Providing code-generating models with type signatures instead of verbose JSON schemas.
- Exposing registered MCP tools as typed methods in sandbox scripts.
- Filtering and searching tools by name or description with `harness_search`.

## Steps
1. **Extract MCP tool definitions**:
   - For each active server in Gateway, retrieve the list of tools and their `inputSchema`.
2. **Convert JSON Schema to TypeScript types**:
   - Map primitive types: `string` -> `string`, `number`/`integer` -> `number`, `boolean` -> `boolean`, `null` -> `null`.
   - Map complex types: `array` -> `T[]`, `object` -> `{ [prop: string]: T }` or structured interfaces with optional (`?`) markers based on `required` arrays.
   - Map `enum` types to union literals: `'val1' | 'val2'`.
   - Preserve `description` fields as JSDoc comments preceding properties and methods.
3. **Assemble namespaces and exports**:
   - Wrap each server's tools into a typed namespace or interface:
     ```typescript
     export interface ServerTools {
       tool_name(args: ToolArgs): Promise<unknown>;
     }
     ```
   - Export top-level global `mcp` object declaration:
     ```typescript
     declare const mcp: {
       [serverName: string]: ServerTools;
     };
     ```
4. **Search and compaction**:
   - Implement `search(query)` over tool names, descriptions, and server names.
   - Return only matched `.d.ts` blocks to minimize context token usage.

## How to verify
1. Run unit tests against representative MCP schemas (nested objects, arrays, enums, optional fields).
2. Validate generated TypeScript definitions with a TypeScript compiler or syntax parser.
3. Verify JSDoc descriptions match the tool descriptions.

## Facts found here
- MCP schemas strictly adhere to JSON Schema draft-07.
- Input schemas always specify `type: "object"` at the root for tool argument dictionaries.

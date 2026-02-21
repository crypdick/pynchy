// Wrapper for mcp/gdrive that:
// 1. Patches OAuth2 to include client credentials for automatic token refresh
// 2. Intercepts unsupported MCP methods (resources/list) that crash Claude SDK
//
// Without the OAuth2 patch, the bare `new OAuth2()` in dist/index.js creates
// a client that can't refresh expired access tokens.
//
// Without the resources/list intercept, Claude Code SDK calls resources/list
// during MCP init; the gdrive server returns error -32603, and the SDK marks
// the entire server as "failed" even though tools work fine.
import fs from "fs";
import path from "path";
import { PassThrough } from "stream";
import { createInterface } from "readline";
import { google } from "googleapis";

// --- Intercept stdin to handle unsupported MCP methods ---
// The MCP stdio transport is newline-delimited JSON-RPC. We proxy stdin
// to catch methods the gdrive server can't handle and respond directly.
const realStdin = process.stdin;
const proxyStdin = new PassThrough();
Object.defineProperty(process, "stdin", {
  value: proxyStdin,
  writable: true,
  configurable: true,
});

const rl = createInterface({ input: realStdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  try {
    const msg = JSON.parse(line);
    if (msg.method === "resources/list") {
      // gdrive doesn't support resources — return empty list
      process.stdout.write(
        JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { resources: [] } }) + "\n"
      );
      return;
    }
    if (msg.method === "resources/templates/list") {
      process.stdout.write(
        JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { resourceTemplates: [] } }) + "\n"
      );
      return;
    }
  } catch {
    // Not valid JSON — forward as-is
  }
  proxyStdin.write(line + "\n");
});
rl.on("close", () => proxyStdin.end());

// --- OAuth2 monkey-patch ---
const keyfilePath =
  process.env.GDRIVE_OAUTH_PATH ||
  path.join(
    path.dirname(new URL(import.meta.url).pathname),
    "../../../gcp-oauth.keys.json"
  );

if (fs.existsSync(keyfilePath)) {
  const keys = JSON.parse(fs.readFileSync(keyfilePath, "utf-8"));
  const client = keys.installed || keys.web;

  // Monkey-patch: when dist/index.js calls `new google.auth.OAuth2()` with
  // no args, inject client_id/client_secret so token refresh works.
  const Orig = google.auth.OAuth2;
  google.auth.OAuth2 = function (...args) {
    if (args.length === 0) {
      console.error("[gdrive-wrapper] OAuth2 patch applied — injecting client credentials");
      return new Orig(
        client.client_id,
        client.client_secret,
        client.redirect_uris?.[0]
      );
    }
    return new Orig(...args);
  };
  Object.setPrototypeOf(google.auth.OAuth2, Orig);
  google.auth.OAuth2.prototype = Orig.prototype;
} else {
  console.error(`[gdrive-wrapper] OAuth keyfile not found at ${keyfilePath} — token refresh will not work`);
}

// Import the original server — runs loadCredentialsAndRunServer() on load.
await import("./dist/index.js");

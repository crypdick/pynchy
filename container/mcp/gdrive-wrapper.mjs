// Wrapper for mcp/gdrive that patches OAuth2 to include client credentials,
// enabling automatic token refresh. Without this, the bare `new OAuth2()`
// in dist/index.js creates a client that can't refresh expired access tokens.
import fs from "fs";
import path from "path";
import { google } from "googleapis";

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
}

// Import the original server — runs loadCredentialsAndRunServer() on load.
await import("./dist/index.js");

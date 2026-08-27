#!/usr/bin/env node
// Verify that the same Playwright MCP command used by ApplyPilot can attach to CDP.

import readline from "node:readline";
import { spawn } from "node:child_process";

const endpoint = process.argv[2];
if (!endpoint?.startsWith("http://127.0.0.1:") && !endpoint?.startsWith("http://localhost:")) {
  throw new Error("usage: check_playwright_mcp_cdp.mjs http://127.0.0.1:<port>");
}

const command = `npx -y @playwright/mcp@latest --cdp-endpoint=${endpoint} --viewport-size=1024x768`;
const child = spawn(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", command], {
  stdio: ["pipe", "pipe", "pipe"],
});
const pending = new Map();
let nextId = 1;
let stderr = "";

child.stderr.on("data", (chunk) => {
  stderr += chunk.toString();
});

readline.createInterface({ input: child.stdout }).on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
  }
});

function send(payload) {
  child.stdin.write(`${JSON.stringify(payload)}\n`);
}

function request(method, params) {
  const id = nextId++;
  send({ jsonrpc: "2.0", id, method, params });
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const timeout = setTimeout(() => {
  for (const { reject } of pending.values()) reject(new Error("MCP probe timed out"));
  child.kill();
}, 30_000);

try {
  const initialized = await request("initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "applypilot-live-smoke", version: "1.0" },
  });
  send({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
  const listed = await request("tools/list", {});
  const snapshotTool = listed.tools.find((tool) => tool.name === "browser_snapshot");
  if (!snapshotTool) throw new Error("browser_snapshot is unavailable");
  const snapshot = await request("tools/call", {
    name: "browser_snapshot",
    arguments: {},
  });
  const text = (snapshot.content || [])
    .filter((item) => item.type === "text")
    .map((item) => item.text)
    .join("\n");
  console.log(JSON.stringify({
    ok: true,
    server: initialized.serverInfo,
    toolCount: listed.tools.length,
    snapshotContainsWebForm: text.includes("Web form"),
    snapshotExcerpt: text.slice(0, 600),
  }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ ok: false, error: String(error), stderr }, null, 2));
  process.exitCode = 1;
} finally {
  clearTimeout(timeout);
  child.stdin.end();
  child.kill();
}

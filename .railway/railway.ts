import { defineRailway, project, service } from "railway/iac";

// empire-platform owns only its own services in the shared project.
// Variables stay unmanaged: secrets live in Railway, never in this file.
// Every service builds from the repository Dockerfile and picks its process
// from EMPIRE_SERVICE (api, publisher, mcp, recurring, or rotate); every service but the
// two crons answers /health.
export const partial = "empire-platform";

export default defineRailway(() => {
  const tasksApi = service("tasks-api", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });
  const tasksPublisher = service("tasks-publisher", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });

  // Remote MCP (Streamable HTTP at /mcp) behind Clerk OAuth; see docs/runbooks/remote-mcp.md.
  const empireMcp = service("empire-mcp", {
    healthcheck: "/health",
    healthcheckTimeout: 60,
  });

  // Daily cron run (EMPIRE_SERVICE=recurring): generates the due instances of recurring
  // tasks through the Tasks API, then exits. 15 17 * * * UTC = 00:15 Asia/Bangkok, the
  // schedule of the retired local "Generate Recurring" job. No healthcheck: it is not a server.
  const recurringGen = service("recurring-gen", {
    startCommand: "empire-recurring",
    deploy: { cronSchedule: "15 17 * * *", restartPolicyType: "NEVER" },
  });

  // Weekly cron run (EMPIRE_SERVICE=rotate): renews the automation tokens due within 10
  // days with the signing key from Infisical and pushes them to Railway, Vercel, the remote
  // MCP Worker and Infisical (empire_platform.hosted_rotate), then reports the heartbeat
  // token-rotate. 30 19 * * 0 UTC = Monday 02:30 Asia/Bangkok, the old crontab slot.
  const tokenRotate = service("token-rotate", {
    startCommand: "empire-token-rotate-hosted",
    deploy: { cronSchedule: "30 19 * * 0", restartPolicyType: "NEVER" },
  });

  return project("platform-empire", {
    variables: { managed: false },
    resources: [tasksApi, tasksPublisher, empireMcp, recurringGen, tokenRotate],
  });
});

// Optional local preview for environments that cannot fork the Next CLI.
// Only HTTP transport is hosted here; all business requests still go to FastAPI.
import { createServer } from "node:http";
import next from "next";
const hostname = "127.0.0.1",
  port = Number(process.env.PORT || 3000);
const app = next({
  dev: true,
  hostname,
  port,
  webpack: true,
  turbopack: false,
});
await app.prepare();
const server = createServer(app.getRequestHandler());
server.listen(port, hostname, () =>
  console.log(`Autumn Prelude preview: http://${hostname}:${port}`),
);
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () =>
    server.close(() => app.close().then(() => process.exit(0))),
  );

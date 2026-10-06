import type { NextConfig } from "next";
const config: NextConfig = {
  allowedDevOrigins: (process.env.DEV_ALLOWED_ORIGINS || "")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean),
  poweredByHeader: false,
  devIndicators: false,
  experimental: {
    workerThreads: true,
    cpus: 2,
    webpackBuildWorker: false,
    useTypeScriptCli: false,
  },
  async rewrites() {
    return process.env.NEXT_PUBLIC_API_MODE === "api"
      ? [
          {
            source: "/api/:path*",
            destination: `${process.env.FASTAPI_ORIGIN || "http://127.0.0.1:8000"}/api/:path*`,
          },
        ]
      : [];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};
export default config;

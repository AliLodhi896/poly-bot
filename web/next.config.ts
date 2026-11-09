import type { NextConfig } from "next";

// The dashboard reads the live bot's snapshot from the FastAPI server started by
// `polybot reward-mm-live` (default http://127.0.0.1:8080). We proxy /api/* to it so
// the browser talks to a single origin in dev and prod — no CORS, no bot changes.
const BACKEND = process.env.POLYBOT_API_URL ?? "http://127.0.0.1:8080";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;

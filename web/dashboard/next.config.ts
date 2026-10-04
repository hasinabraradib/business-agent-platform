import type { NextConfig } from "next";

const config: NextConfig = {
  output: "standalone", // a small self-contained server for the Docker image
  poweredByHeader: false,
  reactStrictMode: true,
  images: { unoptimized: true }, // no image optimiser is used...
  // ...so sharp and its LGPL libvips binaries are kept out of the standalone server.
  outputFileTracingExcludes: { "*": ["node_modules/sharp/**", "node_modules/@img/**"] },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default config;

import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The floating "N" dev-tools button reads as part of the product in local previews.
  devIndicators: false,
  // Old or guessed addresses for screens that live elsewhere.
  async redirects() {
    return [
      { source: "/access", destination: "/admin", permanent: false },
      { source: "/agents", destination: "/runs", permanent: false },
      { source: "/decisions", destination: "/memory", permanent: false },
      { source: "/rules", destination: "/memory?view=rules", permanent: false },
      { source: "/activity", destination: "/logs", permanent: false },
    ];
  },
};

export default nextConfig;

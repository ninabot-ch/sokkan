/** SOKKAN web — proxy /api/* to the FastAPI backend so the browser only talks
 *  to the Next origin (no CORS, backend stays loopback). */
const backend = process.env.SOKKAN_API || "http://127.0.0.1:8097";

/** @type {import('next').NextConfig} */
export default {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      // terminal : passe par le backend (proxy authentifié vers ttyd), plus ttyd direct
      { source: "/term/:path*", destination: `${backend}/term/:path*` },
    ];
  },
};

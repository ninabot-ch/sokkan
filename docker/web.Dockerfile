# SOKKAN web — Next.js (proxy /api vers le backend, cf. next.config.mjs)
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend .
# next fige next.config au build → l'URL du backend doit être connue ici
ENV SOKKAN_API=http://api:8097
RUN npm run build

# image finale minimale : le serveur standalone de Next (pas de node_modules complets)
FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]

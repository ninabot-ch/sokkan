# SOKKAN web — Next.js (proxy /api vers le backend, cf. next.config.mjs)
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend .
# next fige next.config au build → l'URL du backend doit être connue ici
ENV SOKKAN_API=http://api:8097
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app ./
EXPOSE 3000
CMD ["npx", "next", "start", "-p", "3000", "-H", "0.0.0.0"]

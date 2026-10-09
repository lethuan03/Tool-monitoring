# --- Build stage: install backend deps ---
FROM node:20-bookworm-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

# --- Runtime stage ---
FROM node:20-bookworm-slim
WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000

COPY --from=build /app/node_modules ./node_modules
COPY . .

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://localhost:'+(process.env.PORT||3000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/server.js"]

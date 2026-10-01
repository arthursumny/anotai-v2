"""AnotAI web — Dockerfile multi-stage.

Estágio 1 (build): node + vite produz dist/.
Estágio 2 (runtime): nginx serve dist/ e faz reverse proxy de /api/* para a API.
"""

FROM node:20-alpine AS build
WORKDIR /build
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /app
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]

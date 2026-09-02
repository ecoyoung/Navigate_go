FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html ./
COPY tsconfig.json ./
COPY vite.config.ts ./
COPY vite.config.docker.ts ./
COPY src ./src
COPY app ./app
COPY public ./public
COPY server ./server

RUN npx vue-tsc --noEmit && npx vite build --config vite.config.docker.ts

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/client /usr/share/nginx/html

EXPOSE 80 3000 8000 8080

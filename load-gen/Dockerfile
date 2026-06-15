FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY index.js ./

RUN addgroup -S lggroup && adduser -S lguser -G lggroup
USER lguser

CMD ["node", "index.js"]

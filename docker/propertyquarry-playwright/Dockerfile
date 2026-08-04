FROM mcr.microsoft.com/playwright:v1.62.1-noble@sha256:dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e

LABEL org.opencontainers.image.title="PropertyQuarry Playwright worker"
LABEL org.opencontainers.image.description="Pinned browser runtime for governed PropertyQuarry tour automation"
LABEL org.opencontainers.image.version="1.62.1"

WORKDIR /opt/propertyquarry-playwright

ENV NODE_ENV=production \
    NODE_PATH=/opt/propertyquarry-playwright/node_modules \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force

CMD ["node", "-e", "const { chromium } = require('playwright'); console.log(JSON.stringify({playwright: require('playwright/package.json').version, chromium: Boolean(chromium)}));"]

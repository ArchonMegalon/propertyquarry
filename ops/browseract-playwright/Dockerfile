FROM mcr.microsoft.com/playwright:v1.62.1-noble@sha256:c091b21d9fae78c76e85cd4356431e9b018402f172a214fc7d7a5e9a7e29d8ac

USER root
WORKDIR /opt/propertyquarry-browser-operator

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm audit --omit=dev --audit-level=high \
    && npm cache clean --force \
    && chown -R pwuser:pwuser /opt/propertyquarry-browser-operator

COPY --chown=pwuser:pwuser --chmod=0555 browseract_google_maps_workflow_operator.mjs browseract_google_maps_workflow_operator.test.mjs ./

ENV NODE_ENV=production \
    HOME=/home/pwuser \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

USER pwuser
RUN node --test browseract_google_maps_workflow_operator.test.mjs \
    && rm browseract_google_maps_workflow_operator.test.mjs

ENTRYPOINT ["node", "/opt/propertyquarry-browser-operator/browseract_google_maps_workflow_operator.mjs"]

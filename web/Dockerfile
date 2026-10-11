# Unprivileged nginx: already runs as a non-root user and listens on 8080.
FROM nginxinc/nginx-unprivileged:1.27-alpine

USER root

# Vendor ECharts at build time rather than loading it from a CDN at runtime, so
# the site keeps working with no internet and nothing external can change under
# it. Version-pinned deliberately.
ARG ECHARTS_VERSION=5.5.1
RUN apk add --no-cache curl \
 && mkdir -p /usr/share/nginx/html/vendor \
 && curl -fsSL -o /usr/share/nginx/html/vendor/echarts.min.js \
      "https://cdn.jsdelivr.net/npm/echarts@${ECHARTS_VERSION}/dist/echarts.min.js" \
 && test -s /usr/share/nginx/html/vendor/echarts.min.js \
 && apk del curl

COPY index.html styles.css app.js site.webmanifest /usr/share/nginx/html/
COPY favicon.ico favicon-96.png favicon-192.png favicon-512.png apple-touch-icon.png /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf

USER nginx

EXPOSE 8080

# Unprivileged nginx: runs as a non-root user and listens on 8080.
# On gomez, compose runs it as PUID 1003 / PGID 1006 with tmpfs for nginx's
# cache and pid, per the gomez app standard.
FROM nginxinc/nginx-unprivileged:1.27-alpine

ARG VERSION=dev
USER root

# Vendor ECharts at build time rather than loading it from a CDN at runtime, so
# the site keeps working with no internet and nothing external can change under
# it. Version-pinned deliberately.
ARG ECHARTS_VERSION=5.5.1
RUN apk add --no-cache curl \
 && mkdir -p /usr/share/nginx/html/vendor /usr/share/nginx/html/fonts \
 && curl -fsSL -o /usr/share/nginx/html/vendor/echarts.min.js \
      "https://cdn.jsdelivr.net/npm/echarts@${ECHARTS_VERSION}/dist/echarts.min.js" \
 && test -s /usr/share/nginx/html/vendor/echarts.min.js \
 && apk del curl

COPY index.html app.js app.css gomez-ui.css gomez-ui.js manifest.webmanifest /usr/share/nginx/html/
COPY icon.svg favicon.ico favicon-32.png apple-touch-icon.png icon-192.png icon-512.png icon-maskable-512.png /usr/share/nginx/html/
COPY fonts/ /usr/share/nginx/html/fonts/
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Stamp the release into the page (footer and ?v= cache-busters).
RUN v="$VERSION"; case "$v" in sha-*) v="1.1.1 ($(echo "$v" | cut -c1-11))";; v*) v="${v#v}";; esac \
 && sed -i -e "s|__VERSION__|$v|g" -e "s|__BUILD__|$VERSION|g" /usr/share/nginx/html/index.html \
 && chmod -R a+rX /usr/share/nginx/html

USER nginx
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO /dev/null http://127.0.0.1:8080/ || exit 1

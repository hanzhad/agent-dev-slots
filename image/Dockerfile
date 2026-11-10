# The engine as a container. The build context is the repository root:
#   docker build -f image/Dockerfile -t dev-container-engine:local .
FROM alpine:3.24@sha256:ce9e1e6d23f04a0b7593445a062a9fc73ed52f8260b9396f77a958295ad18ee7

# Only what image/tools.txt lists, so a tool joins the image by a reviewed line.
COPY image/tools.txt /tmp/tools.txt
RUN sed 's/#.*//' /tmp/tools.txt | xargs apk add --no-cache && rm /tmp/tools.txt

# bin/ and lib/ stay side by side; the engine finds lib/ through the symlink.
COPY bin /opt/dev-container/bin
COPY lib /opt/dev-container/lib
COPY templates /opt/dev-container/templates
RUN ln -s /opt/dev-container/bin/dev-container /usr/local/bin/dev-container

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["dev-container", "schedule", "run"]

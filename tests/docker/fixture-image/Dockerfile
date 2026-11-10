# The image the real tests compose services from: the engine image's own tools
# (git, tar) plus the user `node`, so nothing is pulled but the engine's base.
ARG BASE
FROM ${BASE}
USER root
RUN adduser -D -u 1000 node
USER node
ENTRYPOINT []
CMD ["sleep", "infinity"]

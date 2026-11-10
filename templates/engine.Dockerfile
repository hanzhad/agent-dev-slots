# dev-container-engine template: engine.Dockerfile version: 1
# Seeded once by `adopt`: this file is the project's now and is never rewritten.
# The engine reaches a slot over ssh, so the image must run sshd: a stock node:
# or python: image has none, and the first acquire would end in "sshd never came up".
# Host keys live in the volume /etc/ssh/keys; the lease key is written by the
# engine into /etc/ssh/authorized_keys.d/node.
FROM @BASE_IMAGE@
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server git gh ca-certificates \
 && apt-get clean
RUN id node >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash node
RUN rm -f /etc/ssh/ssh_host_* \
 && mkdir -p /etc/ssh/keys /etc/ssh/authorized_keys.d /etc/ssh/sshd_config.d /run/sshd \
 && chmod 0700 /etc/ssh/keys \
 && printf '%s\n' \
      'AllowUsers node' 'PermitRootLogin no' 'PubkeyAuthentication yes' \
      'PasswordAuthentication no' 'KbdInteractiveAuthentication no' \
      'PermitEmptyPasswords no' 'AllowAgentForwarding no' 'X11Forwarding no' \
      'PermitUserEnvironment no' 'AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u' \
      'HostKey /etc/ssh/keys/ssh_host_ed25519_key' \
      > /etc/ssh/sshd_config.d/10-devcontainer.conf
COPY engine-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh
WORKDIR /workspace

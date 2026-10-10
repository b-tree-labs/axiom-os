#!/usr/bin/env bash
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

# Federation test-node entrypoint.
#
# Responsibilities:
#   1. Ensure the `axiom` user has an SSH authorized_keys with the shared
#      test pubkey from /harness/keys/id_ed25519.pub (mounted read-only).
#   2. Ensure this node has its own SSH host key (regenerated per container
#      unless persisted in a volume) so TOFU/fingerprint work is realistic.
#   3. Leave ~/.axi/identity/ empty — the harness / tests are the ones who
#      call `axi federation init`, so each scenario exercises bootstrap
#      explicitly. Do NOT pre-initialize node identity here.
#   4. Start sshd in the foreground (PID 1 equivalent).
#
# Everything below must be idempotent — compose may restart containers.
set -euo pipefail

# -- SSH host keys --------------------------------------------------------
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A >/dev/null
fi

# -- Authorized keys for the axiom user ----------------------------------
mkdir -p /home/axiom/.ssh
if [ -f /harness/keys/id_ed25519.pub ]; then
    cp /harness/keys/id_ed25519.pub /home/axiom/.ssh/authorized_keys
    chmod 600 /home/axiom/.ssh/authorized_keys
fi
# Convenience: same keypair usable for outbound ssh from the container
if [ -f /harness/keys/id_ed25519 ]; then
    cp /harness/keys/id_ed25519 /home/axiom/.ssh/id_ed25519
    cp /harness/keys/id_ed25519.pub /home/axiom/.ssh/id_ed25519.pub
    chmod 600 /home/axiom/.ssh/id_ed25519
fi
# Trust all compose peers without prompting
cat > /home/axiom/.ssh/config <<'EOF'
Host *
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
EOF
chown -R axiom:axiom /home/axiom/.ssh
chmod 700 /home/axiom/.ssh

# -- Axiom state dirs -----------------------------------------------------
mkdir -p /home/axiom/.axi
chown -R axiom:axiom /home/axiom/.axi

echo "[node_entrypoint] node=${AXIOM_NODE_NAME:-?} host=$(hostname) ready"
exec /usr/sbin/sshd -D -e

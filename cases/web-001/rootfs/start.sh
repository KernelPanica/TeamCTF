#!/bin/sh
set -eu
mkdir -p /run/sshd
ssh-keygen -A
service cyberrange-web start
exec /usr/sbin/sshd -D -e

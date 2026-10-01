FROM ubuntu:20.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 openssh-server sudo iptables procps iproute2 && rm -rf /var/lib/apt/lists/*
COPY rootfs/ /opt/service/
RUN useradd --create-home --shell /bin/bash blue \
    && update-alternatives --set iptables /usr/sbin/iptables-nft \
    && update-alternatives --set ip6tables /usr/sbin/ip6tables-nft \
    && printf 'blue ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/blue \
    && chmod 0440 /etc/sudoers.d/blue \
    && cp /opt/service/cyberrange-web /etc/init.d/cyberrange-web \
    && chmod 0755 /etc/init.d/cyberrange-web /opt/service/start.sh \
    && cp /opt/service/sshd_config /etc/ssh/sshd_config \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /opt/objective /run/sshd
EXPOSE 22 80
CMD ["/opt/service/start.sh"]

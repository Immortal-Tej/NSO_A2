import os
import sys
import time
import subprocess
import requests
from datetime import datetime

FLAVOUR      = "small"
IMAGE_NAME   = "Ubuntu 24.04"
NETWORK_CIDR = "192.168.1.0/24"
DNS_SERVER   = "8.8.8.8"
POLL_SECONDS = 30


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def load_openrc(path: str):
    if not os.path.isfile(path):
        log(f"ERROR: openrc file not found: {path}")
        sys.exit(1)
    if os.environ.get("OS_PASSWORD"):
        log("Using OpenStack credentials from environment.")
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line and not line.startswith("#") and "read" not in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if val:
                    os.environ[key.strip()] = val
    if not os.environ.get("OS_PASSWORD"):
        log("ERROR: OS_PASSWORD not set. Please run: source <openrc> first.")
        sys.exit(1)


def read_servers_conf(path: str = "servers.conf") -> int:
    if not os.path.isfile(path):
        return 3
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    return int(line)
                except ValueError:
                    pass
    return 3


def get_or_allocate_floating_ips(conn, count: int):
    all_fips   = list(conn.network.ips())
    unassigned = [f for f in all_fips if f.fixed_ip_address is None]
    log(f"Checking floating IPs, we have {len(unassigned)} unassigned available.")
    if len(all_fips) >= count:
        log(f"Reusing existing {len(all_fips)} floating IP(s).")
        return all_fips[:count]
    result = list(unassigned)
    needed = count - len(result)
    if needed > 0:
        log(f"Allocating {needed} new floating IP(s).")
        ext_net = conn.network.find_network("External")
        for _ in range(needed):
            fip = conn.network.create_ip(floating_network_id=ext_net.id)
            result.append(fip)
    return result


def wait_for_active(conn, server_id: str, timeout: int = 300):
    deadline = time.time() + timeout
    print("    ", end="", flush=True)
    while time.time() < deadline:
        server = conn.compute.get_server(server_id)
        if server.status == "ACTIVE":
            print(f" [{server.name} done]", end="", flush=True)
            return server
        if server.status == "ERROR":
            print()
            log(f"ERROR: server {server.name} entered ERROR state.")
            sys.exit(1)
        print(".", end="", flush=True)
        time.sleep(5)
    print()
    log(f"ERROR: timeout waiting for server {server_id}")
    sys.exit(1)


def private_ip(server):
    for addrs in server.addresses.values():
        for a in addrs:
            if a["OS-EXT-IPS:type"] == "fixed":
                return a["addr"]
    return None


def floating_ip(server):
    for addrs in server.addresses.values():
        for a in addrs:
            if a["OS-EXT-IPS:type"] == "floating":
                return a["addr"]
    return None


def build_ssh_config(path, bastion_ip, proxy_ip, node_ips, ssh_key, tag):
    lines = [
        f"Host {tag}_bastion",
        f"    HostName {bastion_ip}",
        f"    User ubuntu",
        f"    IdentityFile {ssh_key}",
        f"    StrictHostKeyChecking no",
        f"    UserKnownHostsFile /dev/null",
        "",
        f"Host {tag}_proxy",
        f"    HostName {proxy_ip}",
        f"    User ubuntu",
        f"    IdentityFile {ssh_key}",
        f"    ProxyJump {tag}_bastion",
        f"    StrictHostKeyChecking no",
        f"    UserKnownHostsFile /dev/null",
        "",
    ]
    for i, ip in enumerate(node_ips, 1):
        lines += [
            f"Host {tag}_node{i}",
            f"    HostName {ip}",
            f"    User ubuntu",
            f"    IdentityFile {ssh_key}",
            f"    ProxyJump {tag}_bastion",
            f"    StrictHostKeyChecking no",
            f"    UserKnownHostsFile /dev/null",
            "",
        ]
    with open(path, "w") as f:
        f.write("\n".join(lines))


def write_inventory(path, bastion_ip, proxy_ip, node_ips, tag):
    proxy_jump = f"'-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump=ubuntu@{bastion_ip}'"
    lines = [
        "[bastion]",
        f"{tag}_bastion ansible_host={bastion_ip}",
        "",
        "[proxy]",
        f"{tag}_proxy ansible_host={proxy_ip} ansible_ssh_common_args={proxy_jump}",
        "",
        "[nodes]",
    ]
    for i, ip in enumerate(node_ips, 1):
        lines.append(f"{tag}_node{i} ansible_host={ip} ansible_ssh_common_args={proxy_jump}")
    lines += [
        "",
        "[all:vars]",
        "ansible_user=ubuntu",
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'",
        f"tag={tag}",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run_ansible(inventory, ssh_key, ssh_config, tag,
                proxy_private=None, node_ips=None):
    extra_vars = f"tag={tag}"
    if proxy_private:
        extra_vars += f" proxy_private_ip={proxy_private}"
    if node_ips:
        extra_vars += f" node_ips={','.join(node_ips)}"
    cmd = [
        "ansible-playbook",
        "-i", inventory,
        "--private-key", ssh_key,
        "-e", extra_vars,
        "ansible/site.yml"
    ]
    env = os.environ.copy()
    env["ANSIBLE_SSH_ARGS"] = f"-F {os.path.abspath(ssh_config)} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    log(f"Running playbook.")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        log("ERROR: Ansible playbook failed.")
        sys.exit(1)


def validate_service(proxy_ip: str, port: int = 5000, attempts: int = 4):
    url = f"http://{proxy_ip}:{port}/"
    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=5)
            log(f"Request{i}: {r.text.strip()}")
        except Exception as e:
            log(f"Request{i}: FAILED ({e})")
    log("OK")

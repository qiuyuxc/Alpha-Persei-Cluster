"""Resolve listener ports, advertised addresses and CDN egress independently."""
import argparse
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Optional

LOCAL_HTTPS_PORT = 8443
LOCAL_HTTP_PORT = 8080
_HELPER_NETWORK = IPv4Network("10.111.0.0/24")


def _ipv4(value):
    try:
        address = IPv4Address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("请填写数字 IPv4 地址") from exc
    if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("请填写可连接的 IPv4 地址")
    return str(address)


def _port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口须为整数") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口须为 0–65535")
    return port


def add_network_arguments(parser):
    parser.add_argument("--android-local", action="store_true",
                        help="同机 Android 模式：本机监听 8443/8080，向游戏下发 127.0.0.1")
    parser.add_argument("--host-ip", type=_ipv4, default=None,
                        help="下发给客户端的 IPv4（默认检测 LAN；同机模式为 127.0.0.1）")
    parser.add_argument("--cdn-source-ip", type=_ipv4, default=None,
                        help="官方 CDN 出站使用的本机真实 IPv4；与客户端下发地址分开")
    parser.add_argument("--https-port", type=_port, default=None,
                        help="HTTPS SDK 端口（默认 443；同机 Android 为 8443）")
    parser.add_argument("--http-port", type=_port, default=None,
                        help="辅助 HTTP 端口（默认 80；同机 Android 为 8080；0=禁用）")
    parser.add_argument("--gw-port", type=_port, default=8102, help="gateway TCP 端口")
    parser.add_argument("--game-port", type=_port, default=8105, help="game TCP 端口")
    parser.add_argument("--battle-port", type=_port, default=6105,
                        help="UDP 动态战斗服端口（0=禁用；sc_54007 下发给客户端）")
    parser.add_argument("--dns", action="store_true", help="启动 UDP/TCP:53 DNS（需端口权限）")


@dataclass(frozen=True)
class StartupNetwork:
    host_ip: str
    bind_host: str
    https_port: int
    http_port: int
    cdn_source_ip: Optional[str]


def resolve_network_options(args, detect_lan):
    local = args.android_local
    https = args.https_port if args.https_port is not None else LOCAL_HTTPS_PORT if local else 443
    http = args.http_port if args.http_port is not None else LOCAL_HTTP_PORT if local else 80
    if not https or not args.gw_port or not args.game_port:
        raise ValueError("HTTPS、gateway 和 game 端口不可为 0")
    tcp_ports = [https, args.gw_port, args.game_port] + ([http] if http else [])
    if len(set(tcp_ports)) != len(tcp_ports):
        raise ValueError("HTTPS、HTTP、gateway 和 game 须使用不同的 TCP 端口")
    if local:
        if args.host_ip and args.host_ip != "127.0.0.1":
            raise ValueError("--android-local 的客户端下发地址须为 127.0.0.1")
        if args.dns:
            raise ValueError("--android-local 使用连接助手 DNS，不启动本机 53 端口")
        if (https, http) != (LOCAL_HTTPS_PORT, LOCAL_HTTP_PORT):
            raise ValueError("当前连接助手的手机本地模式固定连接 8443/8080，请保留这两个端口")
        if any(port < 1024 for port in tcp_ports + ([args.battle_port] if args.battle_port else [])):
            raise ValueError("--android-local 的所有监听端口须大于或等于 1024")
    host = "127.0.0.1" if local else (args.host_ip or _ipv4(detect_lan()))
    # Local mode follows the physical interface when CDN access is actually needed.
    source = args.cdn_source_ip or (None if local else host)
    if source:
        address = IPv4Address(source)
        if address.is_loopback or address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF \
                or address in IPv4Network("198.18.0.0/15") or address in _HELPER_NETWORK:
            raise ValueError("CDN 出站地址须为本机真实网络 IPv4，不能使用回环或 VPN 内部地址")
    return StartupNetwork(host, "127.0.0.1" if local else "0.0.0.0", https, http, source)

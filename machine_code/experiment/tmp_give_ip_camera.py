#!/usr/bin/env python3
"""
tmp_give_ip_camera.py  —  TEMPORARY UTILITY SCRIPT
Assign a temporary IP address to a GigE camera by serial number using
mvIMPACT Acquire's ForceIP command (GigE Vision standard).

The assigned IP is lost on power-cycle. Use IPConfigure or persistent-IP
settings in the camera firmware for a permanent assignment.

Usage:
    python tmp_give_ip_camera.py --serial <SERIAL> --ip <IP>
                                 [--subnet <SUBNET>] [--gateway <GATEWAY>]

    python tmp_give_ip_camera.py --list          # list all visible cameras

Examples:
    python tmp_give_ip_camera.py --list
    python tmp_give_ip_camera.py --serial BF0001 --ip 192.168.0.100
    python tmp_give_ip_camera.py --serial BF0001 --ip 192.168.0.100 \\
        --subnet 255.255.255.0 --gateway 192.168.0.1
"""

import argparse
import socket
import struct
import sys


def ip_to_int(ip: str) -> int:
    """Convert dotted-decimal IP string to 32-bit integer."""
    return struct.unpack("!I", socket.inet_aton(ip))[0]


def int_to_ip(n: int) -> str:
    return socket.inet_ntoa(struct.pack("!I", n))


def list_devices(dev_mgr) -> None:
    count = dev_mgr.deviceCount()
    if count == 0:
        print("No devices found.")
        return

    print(f"\nFound {count} device(s):\n")
    print(f"  {'#':<4} {'Serial':<20} {'Product':<30} {'Family':<20} {'Current IP'}")
    print("  " + "-" * 90)
    for i in range(count):
        dev = dev_mgr.getDevice(i)
        serial = dev.serial.read()
        product = dev.product.read()
        family = dev.family.read()

        # Try to read the current IP via GenICam interface module
        try:
            import mvIMPACT.acquire.GenICam as mvIAGC  # type: ignore

            iface = dev_mgr.getInterface(dev.interfaceID.read())
            im = mvIAGC.InterfaceModule(iface)
            im.gevDeviceSelector.write(i)
            current_ip = int_to_ip(im.gevDeviceIPAddress.read())
        except Exception:
            current_ip = "n/a"

        print(f"  {i:<4} {serial:<20} {product:<30} {family:<20} {current_ip}")
    print()


def force_ip(dev_mgr, serial: str, ip: str, subnet: str, gateway: str) -> bool:
    """
    Send FORCE_IP to the camera identified by serial.
    Returns True on success.
    """
    count = dev_mgr.deviceCount()
    if count == 0:
        print("No devices found.")
        return False

    target_idx = None
    for i in range(count):
        dev = dev_mgr.getDevice(i)
        if dev.serial.read().strip() == serial.strip():
            target_idx = i
            target_dev = dev
            break

    if target_idx is None:
        print(f"Camera with serial '{serial}' not found.")
        print("Run with --list to see available cameras.")
        return False

    print(f"Found camera: {target_dev.product.read()} (serial={serial})")

    try:
        import mvIMPACT.acquire.GenICam as mvIAGC  # type: ignore
    except ImportError:
        print("ERROR: mvIMPACT.acquire.GenICam not available.")
        return False

    try:
        iface_id = target_dev.interfaceID.read()
        iface = dev_mgr.getInterface(iface_id)
        im = mvIAGC.InterfaceModule(iface)

        # Select this device on the interface
        im.gevDeviceSelector.write(target_idx)

        # Set the ForceIP values
        im.gevDeviceForceIPAddress.write(ip_to_int(ip))
        im.gevDeviceForceSubnetMask.write(ip_to_int(subnet))
        im.gevDeviceForceGateway.write(ip_to_int(gateway))

        print("Sending FORCE_IP:")
        print(f"  IP      : {ip}")
        print(f"  Subnet  : {subnet}")
        print(f"  Gateway : {gateway}")

        # Fire the ForceIP command
        im.gevDeviceForceIP.call()

        print("FORCE_IP sent successfully.")
        print("NOTE: This assignment is temporary — it will reset on power-cycle.")
        return True

    except AttributeError as e:
        print(f"ERROR: InterfaceModule attribute not found: {e}")
        print("Your mvIMPACT Acquire version may expose ForceIP differently.")
        print("Try using the IPConfigure GUI tool instead.")
        return False
    except Exception as e:
        print(f"ERROR during ForceIP: {e}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign a temporary IP to a GigE camera by serial number (mvIMPACT Acquire ForceIP)."
    )
    parser.add_argument(
        "--list", action="store_true", help="List all visible cameras and exit."
    )
    parser.add_argument("--serial", help="Camera serial number to target.")
    parser.add_argument("--ip", help="IP address to assign, e.g. 192.168.0.100")
    parser.add_argument(
        "--subnet", default="255.255.255.0", help="Subnet mask (default: 255.255.255.0)"
    )
    parser.add_argument(
        "--gateway", default="0.0.0.0", help="Gateway IP (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    try:
        from mvIMPACT import acquire as mvIA  # type: ignore
    except ImportError:
        print("ERROR: mvIMPACT Acquire Python bindings not installed.")
        print("Install the Impact Acquire package on a warehouse machine.")
        sys.exit(1)

    dev_mgr = mvIA.DeviceManager()

    if args.list:
        list_devices(dev_mgr)
        sys.exit(0)

    if not args.serial or not args.ip:
        parser.error(
            "--serial and --ip are required (or use --list to enumerate cameras)."
        )

    # Validate IP strings before sending
    for label, addr in [
        ("--ip", args.ip),
        ("--subnet", args.subnet),
        ("--gateway", args.gateway),
    ]:
        try:
            socket.inet_aton(addr)
        except OSError:
            parser.error(f"Invalid IP address for {label}: {addr!r}")

    ok = force_ip(dev_mgr, args.serial, args.ip, args.subnet, args.gateway)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

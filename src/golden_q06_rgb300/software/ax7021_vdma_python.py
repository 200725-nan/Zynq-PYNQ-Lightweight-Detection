#!/usr/bin/env python3
"""One-frame AX7021 C1 capture via the verified two-store IOC handoff.

Python 3.4 compatible. This is a bring-up tool, not a detector runner. It
refuses to touch /dev/mem unless the caller asserts that the separately
reviewed test-DTB no-map and CPU/DMA mapping gates have passed. The flag is an
operator assertion; it does not measure page-table memory attributes.
"""
from __future__ import print_function

import argparse
import ctypes
import hashlib
import json
import mmap
import os
import re
import struct
import sys
import time


INPUT_BYTES = 640 * 480 * 3
OUTPUT_BYTES = 300 * 300 * 3
VDMA_BASE = 0x43C00000
VDMA_MAP_BYTES = 4096
DEFAULT_TIMEOUT_NS = 5000000000
VDMA_SR_HALTED = 1 << 0
VDMA_SR_FRAME_IRQ = 1 << 12


class FrameConfig(ctypes.Structure):
    _fields_ = [
        ("reserved_base", ctypes.c_uint64),
        ("reserved_bytes", ctypes.c_uint64),
        ("mm2s_address", ctypes.c_uint64),
        ("s2mm_address", ctypes.c_uint64),
        ("mm2s_hsize", ctypes.c_uint32),
        ("mm2s_stride", ctypes.c_uint32),
        ("mm2s_vsize", ctypes.c_uint32),
        ("s2mm_hsize", ctypes.c_uint32),
        ("s2mm_stride", ctypes.c_uint32),
        ("s2mm_vsize", ctypes.c_uint32),
        ("timeout_ns", ctypes.c_uint64),
    ]


class FrameResult(ctypes.Structure):
    _fields_ = [
        ("mm2s_status", ctypes.c_uint32),
        ("s2mm_status", ctypes.c_uint32),
        ("elapsed_ns", ctypes.c_uint64),
        ("reset_error", ctypes.c_int),
    ]


class TwoStoreConfig(ctypes.Structure):
    _fields_ = [
        ("frame", FrameConfig),
        ("s2mm_address2", ctypes.c_uint64),
    ]


class HandoffResult(ctypes.Structure):
    _fields_ = [
        ("mm2s_status", ctypes.c_uint32),
        ("s2mm_status", ctypes.c_uint32),
        ("current_write_store", ctypes.c_uint32),
        ("completed_store", ctypes.c_uint32),
        ("completed_address", ctypes.c_uint64),
        ("elapsed_ns", ctypes.c_uint64),
        ("reset_error", ctypes.c_int),
    ]


def uint_property(path):
    data = open(path, "rb").read()
    if len(data) != 4:
        raise ValueError("expected one 32-bit device-tree cell: " + path)
    return struct.unpack(">I", data)[0]


def cells(data):
    if len(data) == 0 or (len(data) % 4):
        raise ValueError("malformed device-tree cell array")
    return struct.unpack(">" + "I" * (len(data) // 4), data)


def parse_reserved_node(node_name):
    if not node_name or node_name in (".", "..") or "/" in node_name or "\\" in node_name:
        raise ValueError("reserved node must be a single device-tree node name")
    root = "/proc/device-tree/reserved-memory"
    node = os.path.join(root, node_name)
    status_path = os.path.join(node, "status")
    if os.path.isfile(status_path):
        with open(status_path, "rb") as stream:
            status = stream.read().split(b"\0", 1)[0]
        if status not in (b"okay", b"ok"):
            raise ValueError("reserved-memory node status is not enabled: %s" %
                             status.decode("ascii", "replace"))
    addr_cells = uint_property(os.path.join(root, "#address-cells"))
    size_cells = uint_property(os.path.join(root, "#size-cells"))
    if addr_cells not in (1, 2) or size_cells not in (1, 2):
        raise ValueError("unsupported reserved-memory cell geometry")
    device_tree = "/proc/device-tree"
    root_addr_cells = uint_property(os.path.join(device_tree, "#address-cells"))
    root_size_cells = uint_property(os.path.join(device_tree, "#size-cells"))
    if addr_cells != root_addr_cells or size_cells != root_size_cells:
        raise ValueError("reserved-memory cell geometry does not match device-tree root")
    if not os.path.isfile(os.path.join(root, "ranges")):
        raise ValueError("reserved-memory root lacks ranges property")
    no_map = os.path.join(node, "no-map")
    if not os.path.isfile(no_map) or os.path.getsize(no_map) != 0:
        raise ValueError("reserved-memory node lacks a zero-length no-map property")
    reg = cells(open(os.path.join(node, "reg"), "rb").read())
    tuple_cells = addr_cells + size_cells
    if len(reg) != tuple_cells:
        raise ValueError("expected exactly one reserved-memory range")
    base = 0
    size = 0
    for value in reg[:addr_cells]:
        base = (base << 32) | value
    for value in reg[addr_cells:]:
        size = (size << 32) | value
    if base & 4095:
        raise ValueError("reserved-memory base must be page aligned for mmap")
    if size < INPUT_BYTES + OUTPUT_BYTES or base + size > (1 << 32):
        raise ValueError("reserved-memory range is too small or exceeds 32-bit DMA space")
    return base, size


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def parse_uio_metadata(device_path, sysfs_root="/sys/class/uio"):
    """Validate the C1 UIO identity/map and seed its cumulative event counter."""
    basename = os.path.basename(device_path.rstrip("/"))
    match = re.match(r"^uio([0-9]+)$", basename)
    if not match:
        raise ValueError("UIO device must be named /dev/uioN")
    node = os.path.join(sysfs_root, basename)
    driver_link = os.path.join(node, "device", "driver")
    if not os.path.islink(driver_link):
        raise ValueError("UIO device is not bound to a driver")
    driver_name = os.path.basename(os.path.realpath(driver_link))
    if driver_name != "uio_pdrv_genirq":
        raise ValueError("unexpected UIO driver: " + driver_name)

    def read_text(relative):
        with open(os.path.join(node, relative), "r") as stream:
            return stream.read().strip()

    name = read_text("name")
    address = int(read_text("maps/map0/addr"), 0)
    size = int(read_text("maps/map0/size"), 0)
    event_count = int(read_text("event"), 0)
    if name != "c1-s2mm-irq":
        raise ValueError("unexpected UIO name: " + name)
    if address != VDMA_BASE:
        raise ValueError("UIO map0 base does not match C1 VDMA: 0x%x" % address)
    if size < VDMA_MAP_BYTES:
        raise ValueError("UIO map0 is too short for VDMA registers")
    if event_count < 0 or event_count > 0xFFFFFFFF:
        raise ValueError("UIO event counter is outside the 32-bit ABI")
    return {"index": int(match.group(1)), "name": name, "addr": address,
            "size": size, "event_count": event_count,
            "driver": driver_name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reserved-node", required=True,
                        help="node name under /proc/device-tree/reserved-memory")
    parser.add_argument("--input-offset", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--output-offset", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--output2-offset", type=lambda s: int(s, 0),
                        help="second S2MM frame-store offset in the no-map reserved-memory range")
    parser.add_argument("--input-rgb", required=True)
    parser.add_argument("--output-rgb", required=True)
    parser.add_argument("--lib", default="./libax7021_vdma.so")
    parser.add_argument("--uio-device",
                        help="optional /dev/uioN for S2MM interrupt-driven wait")
    parser.add_argument("--uio-sysfs-root", default="/sys/class/uio",
                        help="UIO sysfs root; override only for a test fixture")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--uncached-map-confirmed", action="store_true",
                        help="operator assertion only; this program does not measure page-table attributes")
    parser.add_argument("--run-on-board", action="store_true")
    parser.add_argument("--result-json", default="ax7021_vdma_frame_result.json")
    args = parser.parse_args()
    if (ctypes.sizeof(FrameConfig) != 64 or ctypes.sizeof(FrameResult) != 24 or
            ctypes.sizeof(TwoStoreConfig) != 72 or ctypes.sizeof(HandoffResult) != 40):
        parser.error("ctypes ABI layout does not match the ARM C helper interface")
    if not args.uncached_map_confirmed or not args.run_on_board:
        parser.error("requires --uncached-map-confirmed and --run-on-board after board mapping gate")
    if args.timeout_ms <= 0:
        parser.error("timeout-ms must be positive")
    if args.output2_offset is None:
        parser.error("--output2-offset is required by the verified two-store handoff API")

    source = open(args.input_rgb, "rb").read()
    if len(source) != INPUT_BYTES:
        raise ValueError("input RGB must be exactly %d bytes" % INPUT_BYTES)
    if args.input_offset < 0 or args.output_offset < 0 or args.output2_offset < 0 or \
       (args.input_offset & 63) or (args.output_offset & 63) or (args.output2_offset & 63):
        raise ValueError("buffer offsets must be nonnegative and 64-byte aligned")

    reserved_base, reserved_size = parse_reserved_node(args.reserved_node)
    if args.input_offset + INPUT_BYTES > reserved_size or \
       args.output_offset + OUTPUT_BYTES > reserved_size or \
       args.output2_offset + OUTPUT_BYTES > reserved_size:
        raise ValueError("frame buffers exceed the reserved-memory node")
    ranges = [(args.input_offset, INPUT_BYTES),
              (args.output_offset, OUTPUT_BYTES),
              (args.output2_offset, OUTPUT_BYTES)]
    for index, (start_a, bytes_a) in enumerate(ranges):
        for start_b, bytes_b in ranges[index + 1:]:
            if start_a < start_b + bytes_b and start_b < start_a + bytes_a:
                raise ValueError("input and two S2MM frame-store buffers must not overlap")

    config = TwoStoreConfig()
    config.frame.reserved_base = reserved_base
    config.frame.reserved_bytes = reserved_size
    config.frame.mm2s_address = reserved_base + args.input_offset
    config.frame.s2mm_address = reserved_base + args.output_offset
    config.s2mm_address2 = reserved_base + args.output2_offset
    config.frame.mm2s_hsize = 1920
    config.frame.mm2s_stride = 1920
    config.frame.mm2s_vsize = 480
    config.frame.s2mm_hsize = 900
    config.frame.s2mm_stride = 900
    config.frame.s2mm_vsize = 300
    config.frame.timeout_ns = args.timeout_ms * 1000000

    uio_info = parse_uio_metadata(args.uio_device, args.uio_sysfs_root) \
        if args.uio_device else None
    mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    uio_fd = os.open(args.uio_device, os.O_RDWR) if args.uio_device else None
    reserved_map = None
    register_map = None
    result_data = None
    try:
        reserved_map = mmap.mmap(mem_fd, reserved_size,
                                 flags=mmap.MAP_SHARED,
                                 prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                 offset=reserved_base)
        if uio_fd is not None:
            register_map = mmap.mmap(uio_fd, VDMA_MAP_BYTES,
                                     flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                     offset=0)
        else:
            register_map = mmap.mmap(mem_fd, VDMA_MAP_BYTES,
                                     flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                     offset=VDMA_BASE)
        region = ctypes.addressof(ctypes.c_char.from_buffer(reserved_map))
        registers = ctypes.addressof(ctypes.c_uint32.from_buffer(register_map))
        ctypes.memmove(region + args.input_offset, source, INPUT_BYTES)
        lib = ctypes.CDLL(args.lib)
        if uio_fd is not None:
            capture_handoff = lib.ax_vdma_capture_one_frame_handoff_uio_mmio
            capture_handoff.argtypes = [ctypes.c_int,
                                        ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.POINTER(TwoStoreConfig),
                                        ctypes.POINTER(HandoffResult)]
            last_event_count = ctypes.c_uint32(uio_info["event_count"])
        else:
            capture_handoff = lib.ax_vdma_capture_one_frame_handoff_mmio
            capture_handoff.argtypes = [ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.POINTER(TwoStoreConfig),
                                        ctypes.POINTER(HandoffResult)]
        capture_handoff.restype = ctypes.c_int
        result = HandoffResult()
        wall_start = time.monotonic()
        if uio_fd is not None:
            rc = capture_handoff(uio_fd,
                ctypes.cast(registers, ctypes.POINTER(ctypes.c_uint32)),
                ctypes.byref(last_event_count), ctypes.byref(config),
                ctypes.byref(result))
        else:
            rc = capture_handoff(ctypes.cast(registers, ctypes.POINTER(ctypes.c_uint32)),
                                 ctypes.byref(config), ctypes.byref(result))
        wall_elapsed = time.monotonic() - wall_start
        if rc != 0:
            raise RuntimeError("VDMA handoff failed rc=%d mm2s=0x%08x s2mm=0x%08x reset_error=%d" %
                               (rc, result.mm2s_status, result.s2mm_status,
                                result.reset_error))
        if result.current_write_store != 1 or result.completed_store != 0:
            raise RuntimeError("unexpected two-store IOC pointer current=%d completed=%d" %
                               (result.current_write_store, result.completed_store))
        if not (result.mm2s_status & VDMA_SR_HALTED) or \
           not (result.s2mm_status & VDMA_SR_HALTED) or \
           not (result.s2mm_status & VDMA_SR_FRAME_IRQ):
            raise RuntimeError("helper returned before IOC and both VDMA channels halted")
        completed_offset = result.completed_address - reserved_base
        expected_completed_offset = (args.output_offset if result.completed_store == 0
                                     else args.output2_offset)
        if completed_offset != expected_completed_offset:
            raise RuntimeError("completed store index/address do not identify the same configured buffer")
        output = ctypes.string_at(region + completed_offset, OUTPUT_BYTES)
        with open(args.output_rgb, "wb") as out:
            out.write(output)
        result_data = {
            "scope": "two-store IOC previous-frame handoff; not detector inference or FPS",
            "reserved_base": hex(reserved_base),
            "reserved_size": reserved_size,
            "input_address": hex(config.frame.mm2s_address),
            "output_address": hex(config.frame.s2mm_address),
            "output2_address": hex(config.s2mm_address2),
            "completed_store_index": result.completed_store,
            "current_write_store_index": result.current_write_store,
            "completed_address": hex(result.completed_address),
            "input_bytes": len(source),
            "output_bytes": len(output),
            "input_sha256": sha256_bytes(source),
            "output_sha256": sha256_bytes(output),
            "mm2s_status": "0x%08x" % result.mm2s_status,
            "s2mm_status": "0x%08x" % result.s2mm_status,
            "reset_error": result.reset_error,
            "vdma_elapsed_ns": result.elapsed_ns,
            "host_wall_elapsed_s": wall_elapsed,
        }
        if uio_info is not None:
            result_data["uio"] = dict(uio_info)
            result_data["uio"]["event_count_after"] = last_event_count.value
            result_data["uio"]["interrupt_wait"] = "poll + UIO cumulative event counter"
    finally:
        if register_map is not None:
            register_map.close()
        if reserved_map is not None:
            reserved_map.close()
        if uio_fd is not None:
            os.close(uio_fd)
        os.close(mem_fd)
    with open(args.result_json, "w") as out:
        json.dump(result_data, out, indent=2, sort_keys=True)
        out.write("\n")
    print("AX7021_C1_VDMA_HANDOFF_OK output_sha256=" + result_data["output_sha256"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (IOError, OSError, ValueError, RuntimeError) as error:
        sys.exit("AX7021_C1_VDMA_HANDOFF_FAIL: %s" % error)

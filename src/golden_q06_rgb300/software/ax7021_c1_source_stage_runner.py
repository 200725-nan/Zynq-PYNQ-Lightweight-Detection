#!/usr/bin/env python3
"""Run fixed RGB frames through guarded C1 VDMA handoff and ARM ORT.

Python 3.4 compatible. This is a stop-at-IOC fixed-frame validation runner,
not a continuous capture or FPS benchmark. It reuses the C0 stage schema and
detector bridge so the same frozen inputs/model can be compared later.
"""
from __future__ import print_function

import argparse
import ctypes
import glob
import hashlib
import json
import mmap
import os
import sys
import time

import ax7021_c0_source_stage_runner as c0
import ax7021_vdma_python as vdma


class FatalDmaHandoffError(RuntimeError):
    """A failed DMA handoff makes buffer ownership unsafe to reuse."""


TWO_STORE_BIT_SHA256 = "bd2ee7e280f7c09723e75cf9b1492e3e19c7b73a3df37b35b34f20e1b8ea46d8"
TWO_STORE_XSA_SHA256 = "562027eeb5e51694fb1bb03faa78f4dc73de343115c210d816d01ce87852246e"


def blank_row(run_id, frame_id, generation):
    row = {"run_id": run_id, "frame_id": frame_id,
           "generation": generation, "buffer_id": None,
           "input_sha256": "0" * 64,
           "capture_or_file_ready_ns": c0.now_ns(),
           "result_done_ns": None, "drop_reason": None, "error_code": None,
           "input_buffer_alias": True, "detector_output_sha256": None,
           "pipeline_mode": "C1_fixed_source_stop_at_IOC"}
    for start, end in c0.STAGES:
        row[start], row[end] = None, None
    return row


def make_output_pointer(reserved_map, offset):
    anchor = ctypes.c_uint8.from_buffer(reserved_map, offset)
    pointer = ctypes.cast(ctypes.pointer(anchor),
                          ctypes.POINTER(ctypes.c_uint8))
    return anchor, pointer


def make_frame_config(reserved_base, reserved_size, input_offset,
                      output_offset, output2_offset, timeout_ms):
    config = vdma.TwoStoreConfig()
    config.frame.reserved_base = reserved_base
    config.frame.reserved_bytes = reserved_size
    config.frame.mm2s_address = reserved_base + input_offset
    config.frame.s2mm_address = reserved_base + output_offset
    config.s2mm_address2 = reserved_base + output2_offset
    config.frame.mm2s_hsize = 1920
    config.frame.mm2s_stride = 1920
    config.frame.mm2s_vsize = 480
    config.frame.s2mm_hsize = 900
    config.frame.s2mm_stride = 900
    config.frame.s2mm_vsize = 300
    config.frame.timeout_ns = timeout_ms * 1000000
    return config


def invoke_handoff(capture, registers, config, uio_fd=None,
                   last_event_count=None):
    result = vdma.HandoffResult()
    if uio_fd is not None:
        if last_event_count is None:
            raise ValueError("UIO handoff requires an event-count seed")
        rc = capture(uio_fd, registers, ctypes.byref(last_event_count),
                     ctypes.byref(config), ctypes.byref(result))
    else:
        rc = capture(registers, ctypes.byref(config), ctypes.byref(result))
    if rc != 0:
        raise FatalDmaHandoffError(
            "VDMA_HANDOFF_%d_mm2s_%08x_s2mm_%08x_reset_%d" %
            (rc, result.mm2s_status, result.s2mm_status, result.reset_error))
    if (result.current_write_store != 1 or result.completed_store != 0 or
            not result.mm2s_status & vdma.VDMA_SR_HALTED or
            not result.s2mm_status & vdma.VDMA_SR_HALTED or
            not result.s2mm_status & vdma.VDMA_SR_FRAME_IRQ):
        raise RuntimeError("VDMA_HANDOFF_STATE_MISMATCH")
    completed_offset = result.completed_address - config.frame.reserved_base
    expected_offset = (config.frame.s2mm_address - config.frame.reserved_base
                       if result.completed_store == 0 else
                       config.s2mm_address2 - config.frame.reserved_base)
    if completed_offset != expected_offset:
        raise RuntimeError("VDMA_STORE_ADDRESS_MISMATCH")
    return result, completed_offset


def validate_frames(paths, manifest, model_path):
    expected = None
    manifest_sha = None
    if manifest:
        expected = c0.load_manifest(manifest, model_path, paths)
        manifest_sha = c0.file_sha256(manifest)
    for path in paths:
        raw = c0.expected_raw_size(path)
        digest = hashlib.sha256(raw).hexdigest()
        if expected is not None and digest != expected[os.path.basename(path)]:
            raise ValueError("input SHA-256 mismatch: " + os.path.basename(path))
    return expected, manifest_sha


def select_frame_paths(paths, max_frames):
    """Select the first N sorted source frames; zero keeps the full frozen set."""
    if max_frames < 0:
        raise ValueError("max_frames must be nonnegative")
    return paths[:max_frames] if max_frames else paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--frame-dir", required=True,
                        help="same frozen 640x480 RGB888 .rgb frame directory as C0")
    parser.add_argument("--max-frames", type=int, default=0,
                        help="run only the first N sorted manifest frames; 0 runs all frames")
    parser.add_argument("--manifest", required=True,
                        help="frozen source manifest; model and every frame SHA are checked")
    parser.add_argument("--lib", default="./libax7021_vdma.so")
    parser.add_argument("--uio-device",
                        help="optional /dev/uioN for S2MM interrupt-driven wait")
    parser.add_argument("--uio-sysfs-root", default="/sys/class/uio",
                        help="UIO sysfs root; override only for host fixtures")
    parser.add_argument("--bit", required=True,
                        help="routed C1 two-store BIT from the frozen candidate build")
    parser.add_argument("--xsa", required=True,
                        help="matching two-store XSA from the frozen candidate build")
    parser.add_argument("--programmed-bit-sha256", required=True,
                        help="operator attestation of the BIT just programmed to PL")
    parser.add_argument("--reserved-node", required=True)
    parser.add_argument("--input-offset", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--output-offset", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--output2-offset", type=lambda s: int(s, 0), required=True)
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--metadata-json")
    parser.add_argument("--output-capture-dir",
                        help="optional post-inference RGB capture for bytewise Q0.6 golden comparison")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--intra", type=int, default=0)
    parser.add_argument("--inter", type=int, default=1)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runtime-version", default="unrecorded")
    parser.add_argument("--runtime-library", help="optional ORT shared-library SHA provenance")
    parser.add_argument("--uncached-map-confirmed", action="store_true",
                        help="operator assertion only; this program does not measure page-table attributes")
    parser.add_argument("--run-on-board", action="store_true")
    args = parser.parse_args()

    if not args.uncached_map_confirmed or not args.run_on_board:
        parser.error("requires --uncached-map-confirmed and --run-on-board after live DTB/cache gate")
    if (args.timeout_ms <= 0 or args.intra < 0 or args.inter < 0 or
            args.warmups < 0 or args.max_frames < 0):
        parser.error("timeout must be positive; thread counts, warmups, and max-frames nonnegative")
    for label, path in (("bridge", args.bridge), ("model", args.model),
                        ("manifest", args.manifest), ("VDMA library", args.lib),
                        ("two-store BIT", args.bit), ("two-store XSA", args.xsa)):
        if not os.path.isfile(path):
            parser.error("%s file missing" % label)
    if args.runtime_library and not os.path.isfile(args.runtime_library):
        parser.error("runtime-library file missing")

    paths = sorted(glob.glob(os.path.join(args.frame_dir, "*.rgb")))
    if not paths:
        parser.error("no RGB frames in frame-dir")
    try:
        expected, manifest_sha = validate_frames(paths, args.manifest, args.model)
    except (IOError, OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    manifest_frame_count = len(expected) if expected is not None else None
    paths = select_frame_paths(paths, args.max_frames)
    bit_sha = c0.file_sha256(args.bit)
    xsa_sha = c0.file_sha256(args.xsa)
    if bit_sha.lower() != TWO_STORE_BIT_SHA256 or \
            args.programmed_bit_sha256.lower() != TWO_STORE_BIT_SHA256:
        parser.error("BIT is not the frozen two-store candidate or programmed-bit attestation differs")
    if xsa_sha.lower() != TWO_STORE_XSA_SHA256:
        parser.error("XSA does not match the frozen two-store candidate")

    offsets = ((args.input_offset, vdma.INPUT_BYTES),
               (args.output_offset, vdma.OUTPUT_BYTES),
               (args.output2_offset, vdma.OUTPUT_BYTES))
    if any(offset < 0 or offset & 63 for offset, unused_size in offsets):
        parser.error("all buffer offsets must be nonnegative and 64-byte aligned")
    for index, (start_a, size_a) in enumerate(offsets):
        for start_b, size_b in offsets[index + 1:]:
            if start_a < start_b + size_b and start_b < start_a + size_a:
                parser.error("input and two S2MM frame-store buffers must not overlap")

    bridge = c0.configure_bridge(args.bridge)
    init_start = c0.now_ns()
    rc = bridge.ortpy_init_config(args.model.encode("utf-8"), args.intra, args.inter)
    init_end = c0.now_ns()
    if rc != 0:
        message = c0.error_text(bridge)
        bridge.ortpy_close()
        parser.error("ORT_INIT: " + message)

    vdma_lib = None
    mem_fd = None
    reserved_map = None
    register_map = None
    registers_word = None
    registers = None
    anchor = None
    warmup_pixels = None
    pixels = None
    uio_fd = None
    uio_info = None
    last_event_count = None
    completed = 0
    errors = 0
    capture_errors = 0
    warmup_ns = 0
    warmup_error = None
    try:
        reserved_base, reserved_size = vdma.parse_reserved_node(args.reserved_node)
        if reserved_base + reserved_size > (1 << 32):
            raise ValueError("reserved range exceeds 32-bit VDMA address space")
        for offset, size in offsets:
            if offset + size > reserved_size:
                raise ValueError("configured frame buffer exceeds reserved-memory node")

        if args.uio_device:
            uio_info = vdma.parse_uio_metadata(args.uio_device,
                                               args.uio_sysfs_root)

        # No device file is opened until both manual board gates and DT checks pass.
        mem_fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        reserved_map = mmap.mmap(mem_fd, reserved_size, flags=mmap.MAP_SHARED,
                                 prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                 offset=reserved_base)
        if uio_info is not None:
            uio_fd = os.open(args.uio_device, os.O_RDWR)
            register_map = mmap.mmap(uio_fd, vdma.VDMA_MAP_BYTES,
                                     flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                     offset=0)
        else:
            register_map = mmap.mmap(mem_fd, vdma.VDMA_MAP_BYTES,
                                     flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE,
                                     offset=vdma.VDMA_BASE)
        vdma_lib = ctypes.CDLL(args.lib)
        if uio_info is not None:
            capture = vdma_lib.ax_vdma_capture_one_frame_handoff_uio_mmio
            capture.argtypes = [ctypes.c_int,
                                ctypes.POINTER(ctypes.c_uint32),
                                ctypes.POINTER(ctypes.c_uint32),
                                ctypes.POINTER(vdma.TwoStoreConfig),
                                ctypes.POINTER(vdma.HandoffResult)]
            last_event_count = ctypes.c_uint32(uio_info["event_count"])
        else:
            capture = vdma_lib.ax_vdma_capture_one_frame_handoff_mmio
            capture.argtypes = [ctypes.POINTER(ctypes.c_uint32),
                                ctypes.POINTER(vdma.TwoStoreConfig),
                                ctypes.POINTER(vdma.HandoffResult)]
        capture.restype = ctypes.c_int
        registers_word = ctypes.c_uint32.from_buffer(register_map)
        registers = ctypes.pointer(registers_word)
        region_address = ctypes.addressof(ctypes.c_char.from_buffer(reserved_map))

        warmup_start = c0.now_ns()
        warmup_config = make_frame_config(reserved_base, reserved_size,
                                          args.input_offset, args.output_offset,
                                          args.output2_offset, args.timeout_ms)
        for warmup_index in range(args.warmups):
            warmup_path = paths[warmup_index % len(paths)]
            warmup_raw = c0.expected_raw_size(warmup_path)
            warmup_digest = hashlib.sha256(warmup_raw).hexdigest()
            if expected is not None and warmup_digest != expected[os.path.basename(warmup_path)]:
                raise ValueError("warmup source SHA-256 mismatch")
            ctypes.memmove(region_address + args.input_offset,
                           warmup_raw, len(warmup_raw))
            try:
                unused_result, warmup_offset = invoke_handoff(
                    capture, registers, warmup_config, uio_fd, last_event_count)
            except FatalDmaHandoffError as exc:
                warmup_error = {
                    "warmup_index": warmup_index,
                    "frame_id": os.path.basename(warmup_path),
                    "error_code": "%s: %s" % (type(exc).__name__, exc),
                }
                errors += 1
                print("WARMUP_ERROR frame=%s index=%d %s" %
                      (warmup_error["frame_id"], warmup_index,
                       warmup_error["error_code"]))
                break
            anchor, warmup_pixels = make_output_pointer(reserved_map,
                                                        warmup_offset)
            rc, unused_detections, unused_cpu, message = c0.call_bridge(
                bridge, warmup_pixels)
            del anchor
            if rc != 0:
                raise RuntimeError("WARMUP_ORT_RUN_%d: %s" % (rc, message))
        uio_event_count_after_warmups = (
            int(last_event_count.value) if uio_info is not None else None)
        warmup_ns = c0.now_ns() - warmup_start

        aborted_after_dma_handoff_failure = warmup_error is not None
        frame_paths = [] if warmup_error is not None else paths
        with open(args.output_jsonl, "w") as log:
            for generation, path in enumerate(frame_paths):
                row = blank_row(args.run_id, os.path.basename(path), generation)
                fatal_handoff_error = False
                if uio_info is not None:
                    row["pipeline_mode"] = "C1_fixed_source_stop_at_IOC_UIO"
                    row["uio_event_count_before"] = int(last_event_count.value)
                try:
                    row["input_read_start_ns"] = c0.now_ns()
                    with open(path, "rb") as source_file:
                        raw = source_file.read()
                    row["input_read_end_ns"] = c0.now_ns()
                    row["input_sha256"] = hashlib.sha256(raw).hexdigest()
                    if len(raw) != vdma.INPUT_BYTES:
                        raise ValueError("RAW_RGB_SIZE_MISMATCH")
                    if expected is not None and row["input_sha256"] != expected[os.path.basename(path)]:
                        raise ValueError("INPUT_SHA256_MISMATCH")

                    # Record source staging separately; PL and VDMA are intentionally
                    # not disentangled because the hardware helper returns at IOC.
                    row["ps_input_stage_start_ns"] = c0.now_ns()
                    ctypes.memmove(region_address + args.input_offset, raw, len(raw))
                    row["ps_input_stage_end_ns"] = c0.now_ns()

                    config = make_frame_config(reserved_base, reserved_size,
                                               args.input_offset, args.output_offset,
                                               args.output2_offset, args.timeout_ms)
                    row["dma_start_ns"] = c0.now_ns()
                    result, completed_offset = invoke_handoff(
                        capture, registers, config, uio_fd, last_event_count)
                    row["dma_done_ns"] = c0.now_ns()
                    row["c_helper_dma_elapsed_ns"] = int(result.elapsed_ns)
                    row["buffer_id"] = int(result.completed_store)
                    if uio_info is not None:
                        row["uio_event_count_after"] = int(last_event_count.value)

                    row["tensor_wrap_or_copy_start_ns"] = c0.now_ns()
                    anchor, pixels = make_output_pointer(reserved_map, completed_offset)
                    row["tensor_wrap_or_copy_end_ns"] = c0.now_ns()
                    row["ort_run_start_ns"] = c0.now_ns()
                    rc, detections, cpu_seconds, message = c0.call_bridge(bridge, pixels)
                    row["ort_run_end_ns"] = c0.now_ns()
                    row["ort_cpu_seconds"] = cpu_seconds
                    if rc != 0:
                        raise RuntimeError("ORT_RUN_%d: %s" % (rc, message))

                    row["postprocess_start_ns"] = c0.now_ns()
                    row["detections"] = detections
                    encoded = json.dumps(detections, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")
                    row["detector_output_sha256"] = hashlib.sha256(encoded).hexdigest()
                    row["postprocess_end_ns"] = c0.now_ns()
                    row["result_done_ns"] = c0.now_ns()
                    if args.output_capture_dir:
                        try:
                            if not os.path.isdir(args.output_capture_dir):
                                os.makedirs(args.output_capture_dir)
                            captured = ctypes.string_at(region_address + completed_offset,
                                                        vdma.OUTPUT_BYTES)
                            row["output_rgb_sha256"] = hashlib.sha256(captured).hexdigest()
                            capture_path = os.path.join(args.output_capture_dir,
                                                        os.path.basename(path))
                            with open(capture_path, "wb") as capture_file:
                                capture_file.write(captured)
                        except (IOError, OSError, ValueError) as capture_exc:
                            row["output_capture_error"] = "%s: %s" % (
                                type(capture_exc).__name__, capture_exc)
                    # Keep the ctypes anchor alive until ORT has returned.
                    del anchor
                except (IOError, OSError, ValueError, RuntimeError, MemoryError) as exc:
                    for start, end in c0.STAGES:
                        if (row[start] is None) != (row[end] is None):
                            row[start], row[end] = None, None
                    row["error_code"] = "%s: %s" % (type(exc).__name__, exc)
                    fatal_handoff_error = isinstance(exc, FatalDmaHandoffError)
                if uio_info is not None:
                    row["uio_event_count_after"] = int(last_event_count.value)
                    delta = ((row["uio_event_count_after"] -
                              row["uio_event_count_before"]) & 0xFFFFFFFF)
                    row["uio_irq_events"] = delta
                log.write(json.dumps(row, sort_keys=True) + "\n")
                log.flush()
                if row["error_code"]:
                    errors += 1
                    print("FRAME_ERROR %s %s" % (row["frame_id"], row["error_code"]))
                else:
                    completed += 1
                    if row.get("output_capture_error"):
                        capture_errors += 1
                    print("FRAME_OK %s store=%d detections=%d" %
                          (row["frame_id"], row["buffer_id"], len(row["detections"])))
                if fatal_handoff_error:
                    print("RUN_ABORT_DMA_HANDOFF_FAILURE: buffer ownership is not safe to reuse")
                    aborted_after_dma_handoff_failure = True
                    break
    finally:
        # Drop ctypes exports before closing the mmap objects.
        pixels = None
        warmup_pixels = None
        anchor = None
        registers = None
        registers_word = None
        if register_map is not None:
            register_map.close()
        if uio_fd is not None:
            os.close(uio_fd)
        if reserved_map is not None:
            reserved_map.close()
        if mem_fd is not None:
            os.close(mem_fd)
        bridge.ortpy_close()

    metadata = {
        "run_id": args.run_id,
        "scope": "C1 fixed RGB files -> stop-at-IOC two-store VDMA -> ARM ORT; not continuous capture/FPS",
        "pipeline_mode": "finite two-frame MM2S replay; C helper waits for IOC, stops both channels, then exposes completed store",
        "source_width": c0.SOURCE_WIDTH,
        "source_height": c0.SOURCE_HEIGHT,
        "source_format": "RGB888",
        "output_width": c0.OUTPUT_WIDTH,
        "output_height": c0.OUTPUT_HEIGHT,
        "output_format": "RGB888",
        "model_sha256": c0.file_sha256(args.model),
        "bridge_sha256": c0.file_sha256(args.bridge),
        "vdma_library_sha256": c0.file_sha256(args.lib),
        "bit_sha256": bit_sha,
        "programmed_bit_sha256_attested": args.programmed_bit_sha256.lower(),
        "xsa_sha256": xsa_sha,
        "source_manifest_sha256": manifest_sha,
        "frame_manifest_entries": manifest_frame_count,
        "max_frames": args.max_frames,
        "selected_frame_names": [os.path.basename(path) for path in paths],
        "runtime_version": args.runtime_version,
        "runtime_library_sha256": c0.file_sha256(args.runtime_library) if args.runtime_library else None,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "input_frames": len(paths),
        "completed": completed,
        "errors": errors,
        "aborted_after_dma_handoff_failure": aborted_after_dma_handoff_failure,
        "warmup_error": warmup_error,
        "output_capture_errors": capture_errors,
        "session_init_wall_ns": init_end - init_start,
        "stage_log": os.path.basename(args.output_jsonl),
        "interrupt_wait_mode": "UIO poll/event" if uio_info is not None else "DMASR busy poll",
        "uio": ({"device": args.uio_device, "name": uio_info["name"],
                 "driver": uio_info["driver"],
                 "map0_addr": hex(uio_info["addr"]), "map0_size": uio_info["size"],
                 "event_count_initial": uio_info["event_count"],
                 "event_count_after_warmups": uio_event_count_after_warmups,
                 "event_count_final": int(last_event_count.value)}
                if uio_info is not None else None),
        "output_capture_dir": os.path.abspath(args.output_capture_dir) if args.output_capture_dir else None,
        "output_capture_timing_scope": "optional RGB copy/hash/write occurs after result_done_ns and is excluded from detection latency",
        "pl_timing_scope": "PL and VDMA stream are not separately instrumented; PL remains null and DMA interval covers the C helper IOC transaction",
        "dma_timing_scope": "Python monotonic interval around helper: includes channel setup, finite replay, PL processing, IOC wait and halt; C helper elapsed is also logged",
        "input_stage_timing_scope": "raw file to reserved DDR memmove; logged as ps_input_stage_start/end_ns",
        "cache_sync_scope": "not measured; runner requires a verified uncached no-map reserved mapping before it can be enabled",
        "programmed_bit_scope": "operator attestation only; this process has no PL readback/version register",
        "ort_run_timing_scope": "ctypes bridge call includes ORT tensor construction, ORT Run and output extraction",
        "warmups": args.warmups,
        "warmup_wall_ns": warmup_ns,
        "warmup_policy": "the first N source frames use the same fixed handoff path; both channels halt before the next measured generation, and warmup rows are excluded from the stage log",
    }
    metadata_path = args.metadata_json or args.output_jsonl + ".meta.json"
    parent = os.path.dirname(os.path.abspath(metadata_path))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    with open(metadata_path, "w") as out:
        json.dump(metadata, out, sort_keys=True, indent=2)
        out.write("\n")
    print("C1_SOURCE_RUN input=%d completed=%d errors=%d session_init_ms=%.3f" %
          (len(paths), completed, errors, (init_end - init_start) / 1000000.0))
    return 0 if not errors else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (IOError, OSError, ValueError, RuntimeError) as error:
        sys.exit("AX7021_C1_SOURCE_RUN_FAIL: %s" % error)

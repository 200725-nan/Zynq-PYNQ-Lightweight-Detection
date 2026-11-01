#!/usr/bin/env python3
"""Python 3.4-compatible fixed-source C0 pipeline runner with stage logging."""
import argparse
import ctypes
import glob
import hashlib
import json
import os
import sys
import time


SOURCE_WIDTH = 640
SOURCE_HEIGHT = 480
SOURCE_STRIDE = SOURCE_WIDTH * 3
SOURCE_BYTES = SOURCE_STRIDE * SOURCE_HEIGHT
OUTPUT_WIDTH = 300
OUTPUT_HEIGHT = 300
OUTPUT_STRIDE = OUTPUT_WIDTH * 3
OUTPUT_BYTES = OUTPUT_STRIDE * OUTPUT_HEIGHT

STAGES = (("input_read_start_ns", "input_read_end_ns"),
          ("pl_start_ns", "pl_end_ns"),
          ("dma_start_ns", "dma_done_ns"),
          ("cache_sync_start_ns", "cache_sync_end_ns"),
          ("ps_reconstruct_start_ns", "ps_reconstruct_end_ns"),
          ("ps_resize_start_ns", "ps_resize_end_ns"),
          ("tensor_wrap_or_copy_start_ns", "tensor_wrap_or_copy_end_ns"),
          ("ort_run_start_ns", "ort_run_end_ns"),
          ("postprocess_start_ns", "postprocess_end_ns"))


def now_ns():
    return int(time.monotonic() * 1000000000)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1048576)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def configure_bridge(path):
    bridge = ctypes.CDLL(path)
    bridge.ortpy_last_error.restype = ctypes.c_char_p
    bridge.ortpy_init_config.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_int]
    bridge.ortpy_init_config.restype = ctypes.c_int
    bridge.ortpy_run.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                 ctypes.POINTER(ctypes.c_float),
                                 ctypes.POINTER(ctypes.c_float),
                                 ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                                 ctypes.POINTER(ctypes.c_int)]
    bridge.ortpy_run.restype = ctypes.c_int
    bridge.ortpy_close.argtypes = []
    bridge.ortpy_close.restype = None
    return bridge


def configure_resizer(path):
    library = ctypes.CDLL(path)
    function = library.ax_q16_resize_rgb
    function.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
                         ctypes.c_uint32, ctypes.c_size_t,
                         ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
                         ctypes.c_uint32, ctypes.c_size_t, ctypes.c_size_t]
    function.restype = ctypes.c_int
    return library, function


def error_text(bridge):
    value = bridge.ortpy_last_error()
    if value:
        return value.decode("utf-8", "replace")
    return "unknown bridge error"


def blank_row(run_id, frame_id, generation, mode):
    row = {"run_id": run_id, "frame_id": frame_id,
           "generation": generation, "buffer_id": generation,
           "input_sha256": "0" * 64, "capture_or_file_ready_ns": now_ns(),
           "result_done_ns": None, "drop_reason": None, "error_code": None,
           "input_buffer_alias": mode == "alias",
           "resized_rgb_sha256": None, "detector_output_sha256": None}
    for start, end in STAGES:
        row[start], row[end] = None, None
    return row


def resize_raw(resizer, raw):
    source = bytearray(raw)
    output = bytearray(OUTPUT_BYTES)
    source_type = ctypes.c_uint8 * len(source)
    output_type = ctypes.c_uint8 * len(output)
    source_view = source_type.from_buffer(source)
    output_view = output_type.from_buffer(output)
    rc = resizer(source_view, SOURCE_WIDTH, SOURCE_HEIGHT, SOURCE_STRIDE,
                 output_view, OUTPUT_WIDTH, OUTPUT_HEIGHT, OUTPUT_STRIDE,
                 OUTPUT_BYTES)
    if rc != 0:
        raise ValueError("PS_RESIZE_%d" % rc)
    return output


def make_tensor(resized, mode):
    array_type = ctypes.c_uint8 * len(resized)
    if mode == "alias":
        return array_type.from_buffer(resized)
    return array_type.from_buffer_copy(resized)


def call_bridge(bridge, pixels):
    capacity = 100
    boxes = (ctypes.c_float * (capacity * 4))()
    classes = (ctypes.c_float * capacity)()
    scores = (ctypes.c_float * capacity)()
    count = ctypes.c_int(0)
    cpu_start = time.process_time()
    rc = bridge.ortpy_run(pixels, OUTPUT_BYTES, boxes, classes, scores,
                          capacity, ctypes.byref(count))
    cpu_seconds = time.process_time() - cpu_start
    if rc != 0:
        return rc, None, cpu_seconds, error_text(bridge)
    if count.value < 0 or count.value > capacity:
        return -1, None, cpu_seconds, "invalid detection count %d" % count.value
    detections = []
    for index in range(count.value):
        detections.append({"class_id": int(round(classes[index])),
                           "score": float(scores[index]),
                           "box_yxyx_normalized": [
                               float(boxes[4 * index + item]) for item in range(4)]})
    return 0, detections, cpu_seconds, None


def expected_raw_size(path):
    with open(path, "rb") as stream:
        raw = stream.read()
    if len(raw) != SOURCE_BYTES:
        raise ValueError("RAW_RGB_SIZE_MISMATCH: expected %d bytes, got %d" %
                         (SOURCE_BYTES, len(raw)))
    return raw


def process_frame(bridge, resizer, path, run_id, generation, mode,
                  expected_sha256):
    row = blank_row(run_id, os.path.basename(path), generation, mode)
    try:
        row["input_read_start_ns"] = now_ns()
        with open(path, "rb") as stream:
            raw = stream.read()
        row["input_read_end_ns"] = now_ns()
        row["input_sha256"] = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and row["input_sha256"] != expected_sha256:
            row["error_code"] = "INPUT_SHA256_MISMATCH"
            return row
        if len(raw) != SOURCE_BYTES:
            row["error_code"] = (
                "RAW_RGB_SIZE_MISMATCH: expected %d bytes, got %d" %
                (SOURCE_BYTES, len(raw)))
            return row

        row["ps_resize_start_ns"] = now_ns()
        resized = resize_raw(resizer, raw)
        row["ps_resize_end_ns"] = now_ns()
        row["resized_rgb_sha256"] = hashlib.sha256(resized).hexdigest()

        row["tensor_wrap_or_copy_start_ns"] = now_ns()
        pixels = make_tensor(resized, mode)
        row["tensor_wrap_or_copy_end_ns"] = now_ns()

        row["ort_run_start_ns"] = now_ns()
        rc, detections, cpu_seconds, message = call_bridge(bridge, pixels)
        row["ort_run_end_ns"] = now_ns()
        row["ort_cpu_seconds"] = cpu_seconds
        if rc != 0:
            row["error_code"] = "ORT_RUN_%d: %s" % (rc, message)
            return row

        row["postprocess_start_ns"] = now_ns()
        row["detections"] = detections
        encoded = json.dumps(detections, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        row["detector_output_sha256"] = hashlib.sha256(encoded).hexdigest()
        row["postprocess_end_ns"] = now_ns()
        row["result_done_ns"] = now_ns()
    except (IOError, OSError, ValueError, MemoryError) as exc:
        if row["input_read_end_ns"] is None:
            row["input_read_start_ns"] = None
        row["error_code"] = "%s: %s" % (type(exc).__name__, exc)
    return row


def load_manifest(path, model_path, frame_paths):
    with open(path, "r") as stream:
        manifest = json.load(stream)
    if manifest.get("source_width") != SOURCE_WIDTH or \
            manifest.get("source_height") != SOURCE_HEIGHT or \
            manifest.get("source_format") != "RGB888":
        raise ValueError("source manifest geometry/format mismatch")
    if manifest.get("model_sha256") != file_sha256(model_path):
        raise ValueError("model SHA-256 does not match source manifest")
    expected = {}
    for item in manifest.get("frames", []):
        name = item.get("frame")
        digest = item.get("rgb_sha256")
        if not name or not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid frame entry in source manifest")
        if name in expected:
            raise ValueError("duplicate frame in source manifest: %s" % name)
        expected[name] = digest.lower()
    actual = set(os.path.basename(path) for path in frame_paths)
    if set(expected) != actual:
        raise ValueError("frame directory does not match manifest frame names")
    return expected


def warmup(bridge, resizer, path, expected_sha256):
    raw = expected_raw_size(path)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("warmup source SHA-256 mismatch: %s" % os.path.basename(path))
    resized = resize_raw(resizer, raw)
    pixels = make_tensor(resized, "copy")
    rc, unused_detections, unused_cpu, message = call_bridge(bridge, pixels)
    if rc != 0:
        raise ValueError("warmup ORT_RUN_%d: %s" % (rc, message))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--resize-library", required=True,
                        help="shared library exporting ax_q16_resize_rgb")
    parser.add_argument("--model", required=True)
    parser.add_argument("--frame-dir", required=True,
                        help="directory containing raw 640x480 RGB888 .rgb frames")
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--metadata-json", help="defaults to output-jsonl + .meta.json")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--manifest", help="frozen raw-source manifest")
    parser.add_argument("--input-mode", choices=("copy", "alias"), default="copy")
    parser.add_argument("--intra", type=int, default=1)
    parser.add_argument("--inter", type=int, default=1)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runtime-version", default="unrecorded")
    parser.add_argument("--runtime-library", help="optional ORT shared-library SHA provenance")
    args = parser.parse_args()

    if args.intra < 0 or args.inter < 0 or args.warmups < 0:
        parser.error("thread counts and warmups must be nonnegative")
    for label, path in (("bridge", args.bridge), ("resize library", args.resize_library),
                        ("model", args.model)):
        if not os.path.isfile(path):
            parser.error("%s file missing" % label)
    if args.runtime_library and not os.path.isfile(args.runtime_library):
        parser.error("runtime-library file missing")
    paths = sorted(glob.glob(os.path.join(args.frame_dir, "*.rgb")))
    if not paths:
        parser.error("no RGB frames in frame-dir")
    if args.manifest:
        try:
            expected = load_manifest(args.manifest, args.model, paths)
        except (IOError, OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        manifest_sha = file_sha256(args.manifest)
    else:
        expected = None
        manifest_sha = None

    bridge = configure_bridge(args.bridge)
    resize_library, resizer = configure_resizer(args.resize_library)
    init_start = now_ns()
    rc = bridge.ortpy_init_config(args.model.encode("utf-8"), args.intra, args.inter)
    init_end = now_ns()
    if rc != 0:
        print("INIT_ERROR %s" % error_text(bridge))
        bridge.ortpy_close()
        return 2

    completed = 0
    errors = 0
    try:
        for warmup_index in range(args.warmups):
            path = paths[warmup_index % len(paths)]
            digest = expected.get(os.path.basename(path)) if expected is not None else None
            try:
                warmup(bridge, resizer, path, digest)
            except (IOError, OSError, ValueError, MemoryError) as exc:
                print("WARMUP_ERROR %s" % exc)
                return 2

        output_parent = os.path.dirname(os.path.abspath(args.output_jsonl))
        if not os.path.isdir(output_parent):
            os.makedirs(output_parent)
        with open(args.output_jsonl, "w") as stream:
            for index, path in enumerate(paths):
                digest = expected.get(os.path.basename(path)) if expected is not None else None
                row = process_frame(bridge, resizer, path, args.run_id, index,
                                    args.input_mode, digest)
                stream.write(json.dumps(row, sort_keys=True) + "\n")
                stream.flush()
                if row["error_code"]:
                    errors += 1
                    print("FRAME_ERROR %s %s" %
                          (row["frame_id"], row["error_code"]))
                else:
                    completed += 1
                    print("FRAME_OK %s count=%d" %
                          (row["frame_id"], len(row["detections"])))
    finally:
        bridge.ortpy_close()

    metadata = {
        "run_id": args.run_id,
        "scope": "C0 fixed raw RGB file input; no PL/DMA or camera capture",
        "source_width": SOURCE_WIDTH,
        "source_height": SOURCE_HEIGHT,
        "source_format": "RGB888",
        "output_width": OUTPUT_WIDTH,
        "output_height": OUTPUT_HEIGHT,
        "resize_reference": "center-aligned Q0.16 bilinear; exact C/Python byte agreement checked on host",
        "model_sha256": file_sha256(args.model),
        "bridge_sha256": file_sha256(args.bridge),
        "resize_library_sha256": file_sha256(args.resize_library),
        "source_manifest_sha256": manifest_sha,
        "runtime_version": args.runtime_version,
        "runtime_library_sha256": file_sha256(args.runtime_library) if args.runtime_library else None,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "input_mode": args.input_mode,
        "input_mode_note": "alias maps the writable resized bytearray; copy duplicates resized RGB into a ctypes array",
        "intra_op_threads": args.intra,
        "inter_op_threads": args.inter,
        "warmups": args.warmups,
        "input_frames": len(paths),
        "completed": completed,
        "errors": errors,
        "session_init_wall_ns": init_end - init_start,
        "stage_log": os.path.basename(args.output_jsonl),
        "ort_run_timing_scope": "ctypes bridge call includes tensor creation, ORT Run and output extraction",
        "ps_resize_timing_scope": "source bytes-to-writable buffer, output allocation and C Q0.16 resize",
    }
    metadata_path = args.metadata_json or args.output_jsonl + ".meta.json"
    with open(metadata_path, "w") as stream:
        json.dump(metadata, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print("C0_SOURCE_RUN input=%d completed=%d errors=%d warmups=%d session_init_ms=%.3f" %
          (len(paths), completed, errors, args.warmups,
           (init_end - init_start) / 1000000.0))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

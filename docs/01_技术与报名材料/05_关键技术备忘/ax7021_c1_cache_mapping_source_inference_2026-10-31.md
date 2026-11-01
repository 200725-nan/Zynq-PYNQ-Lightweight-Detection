# C1 `/dev/mem` cache mapping source inference (2026-10-31)

## Board evidence

- The exact FIT kernel config extracted from the deployed kernel is `experiments/data/v1031/live_fit_audit_v2_images_20261031/kernel.config`, SHA-256 `8e829293bb5d5a548c0859dc7f7ef7df9b55dd9d06c6a4b33a29504a207e536a`; it sets `CONFIG_ARM_DMA_MEM_BUFFERABLE=y`.
- The C1 DMA runner opens `/dev/mem` with `O_RDWR | O_SYNC` (`board_transfer/AX7021_C1_TWO_STORE_UIO_2026-10-31/software/ax7021_vdma_python.py`, SHA-256 `b0ff575835f9f563d48a37478cef4e3267f4eb89b58390d5814ced1d9d808515`). Its `--uncached-map-confirmed` switch is only an operator assertion. The matching source-stage runner explicitly says cache sync is not measured (`ax7021_c1_source_stage_runner.py`, SHA-256 `f27cd07f11e75dede5ffc618d7b8275dccec6b94a7ed22a3652c99ba888d4037`); the captured JSONL has null `cache_sync_start_ns` and `cache_sync_end_ns`.
- In the successful run, 270,000 output bytes matched the frozen Q0.6 golden exactly. This is direct evidence that this one run produced the expected bytes; it does not reveal the PTE memory type.

## Versioned source cross-check

Linux upstream v4.9 `arch/arm/mm/mmu.c` implements `phys_mem_access_prot()` under `CONFIG_ARM_DMA_MEM_BUFFERABLE`: invalid PFNs are mapped with `pgprot_noncached`; valid PFNs opened with `O_SYNC` use `pgprot_writecombine`. The v4.9 ARM `pgtable.h` defines `pgprot_writecombine` by selecting `L_PTE_MT_BUFFERABLE`. Thus the reviewed upstream code maps either branch without ordinary write-back data caching (the valid-PFN branch is write-combine/bufferable).

- `https://github.com/torvalds/linux/blob/v4.9/arch/arm/mm/mmu.c#L3372-L3394`
- `https://github.com/torvalds/linux/blob/v4.9/arch/arm/include/asm/pgtable.h#L103-L124`

## Conclusion and limit

This is a source-based mapping inference using the exact embedded config and the runner's actual `O_SYNC` open flag. It materially supports the single-frame cache-safety interpretation and explains why no explicit cache-maintenance timestamps exist. The deployed Xilinx kernel's exact source commit/local patch set has not been matched byte-for-byte, and the live process PTE was not read back. Therefore report the result as a successful single observed transfer with a strong source-based non-writeback mapping inference, not as direct PTE readback or a complete general cache-ownership proof.


# AX7021 physical RESET and original-BIT rollback: static evidence audit (2026-10-31)

## Decision

A documented rollback mechanism exists for a volatile C1 PL/FDT test, conditional on the board's boot straps and original boot media remaining unchanged: the AX7021 core-board RESET key is wired to `PS_POR_B`; Zynq-7000 POR restarts BootROM and clears PL configuration; the verified original `BOOT.BIN` contains the original `top.bit` partition. On the next POR boot, the configured boot source must still select that unchanged image.

This is a **static, conditional recovery path**, not completed fault-injection evidence. The reset has not been exercised after loading C1, the physical boot-mode switch position has not been captured, and a post-reset original-PL identity check has not been recorded. Do not call rollback tested or use this reasoning to waive the DDR/FDT/cache gates.

## Evidence chain

1. The ALINX [AX7021 User Manual](https://alinx.com/public/upload/file/AX7021_User_Manual.pdf), section 2.9, labels the core-board reset button as `PS_POR_B_500`, Zynq pin B5. The documented board is AX7021 / AC7021; confirm the physical board revision before treating the manual as exact board wiring.
2. AMD [UG585, External Reset Signal Pins](https://docs.amd.com/r/en-US/ug585-zynq-7000-SoC-TRM/External-Reset-Signal-Pins) says `PS_POR_B` is the only reset that samples the boot-mode straps. AMD [UG1046, UltraFast Embedded Design Methodology Guide](https://docs.amd.com/api/khub/documents/7KECH4F2hgvQrFavb20mVg/content) describes POR as restarting BootROM and clearing PL configuration. The AX7021 manual's reset-key wiring therefore points to the reset type needed to clear a volatile PL image and restart the boot flow.
3. The captured board `/boot/BOOT.BIN` copy is 3,021,832 B, SHA-256 `b3fce820a72a51d4526a2c525d8bc6cef4c0089ccf15d7bd2d0afe848cfaab8b`. Bootgen PHT parsing locates the original PL `top.bit.0` payload at `[0x19740,0x27b1e0)`, 2,497,184 B, SHA-256 `08faaed7bd094ec6c71c85cae1db35607c6268eb7c1f492e96eff99621961a5e`; the payload is in-bounds and disjoint from FSBL/U-Boot partitions. Sources: `docs/ax7021_bootbin_partition_forensics_2026-10-31.md` and `experiments/data/v1031/bootgen_partition_range_audit_v1_20261031/audit.json`.
4. The exact U-Boot payload embeds the default SD load path `fatload mmc 0 ${netstart} ${kernel_img}` followed by `bootm ${netstart}`; the read-only U-Boot capture lists `image.ub`, `boot.bin`, and the SD/MMC devices. This supports the SD boot path but does not identify the physical boot-mode switch setting at reset. Evidence: `docs/ax7021_bootbin_partition_forensics_2026-10-31.md` and `experiments/data/v1031/uboot_readonly_v12_20261031/`.

## Safe conclusion and remaining physical proof

If all of the following remain true—this is the documented AX7021 hardware revision, the RESET button asserts `PS_POR_B`, the boot straps select the same SD source, and the original `BOOT.BIN` remains byte-identical—then pressing RESET after a **volatile** PL test is expected to clear PL configuration and restart the original boot flow. This is stronger than an assumed reset-button rollback and supports designing a recovery procedure, but it does not prove recovery occurred on this board.

Before accepting physical rollback, a board run must record the active boot source and exact original-file hashes, load only an approved test image after all memory/cache/ownership gates pass, trigger the documented RESET key after a controlled fault, and capture the returning U-Boot/Linux state plus an independent confirmation that the original PL image is running. Preserve the failure log and both before/after hashes. The present no-write boundary remains in force; no SD file was altered and no reset/fault-injection test was run for this audit.


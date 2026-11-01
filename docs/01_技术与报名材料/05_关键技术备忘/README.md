# 板卡操作补充资料

AX7021 的固定帧运行记录见 `../02_C1_实板结果/board_evidence_manifest.json`。本目录仅保留两份与该次运行相关的专项背景：

- `ax7021_c1_cache_mapping_source_inference_2026-10-31.md`：结合内核配置、Linux 映射源码和 runner 的 O_SYNC 设置分析本次 DMA 映射。
- `ax7021_reset_rollback_static_audit_2026-10-31.md`：依据 AX7021 手册、启动文件哈希和 Zynq POR 资料整理复位恢复路径。

板卡操作以负责人确认的当前启动方式和恢复流程为准；本次成功固定帧实验是直接实测证据，专项审计用于解释运行条件和恢复依据。


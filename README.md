# Zynq-PYNQ-Lightweight-Detection
基于 Zynq-7000+PYNQ 的软硬协同低延迟轻量级目标检测系统

## 📌 项目简介
本项目基于 AX7021（Zynq7020）异构开发平台，采用 PL+PS 软硬协同架构：
- PL(FPGA)：实现图像预处理硬件流水线，并行加速图像缩放、归一化
- PS(PYNQ Linux)：基于 ONNX Runtime ARM32 部署 SSD MobileNet v1 轻量检测模型
实现边缘端低延迟实时目标检测，适用于嵌入式智能感知场景。

## 🧰 硬件与软件环境
- 开发板：AX7021（Zynq7020）
- 系统：PYNQ Linux
- 推理框架：ONNX Runtime ARM32
- 模型：SSD MobileNet v1
- 开发工具：Vivado、PYNQ、Python

## 📂 仓库目录说明
Zynq-PYNQ-Lightweight-Detection
├─ README.md         # 项目主页
├─ .gitignore        # 上传过滤规则
├─ docs/             # 技术文档、实验记录、申报材料、交付表单
├─ demo/             # 测试截图、演示素材说明
├─ src/              # FPGA 硬件工程 (hardware)、PYNQ 软件 (software)、运行时文件、测试图像
├─ model/            # 模型校验文件 + 网盘下载指引（模型本体不上传仓库）
└─ assets/           # 框图、实物照片


## 🚀 复现步骤
1. AX7021 烧录 PYNQ 镜像，SD卡启动开发板，通过网线连接电脑
2. 将 `src/hardware/overlay` 下 `.bit` + `.hwh` 文件上传到 PYNQ，加载硬件Overlay
3. 前往 `model/README.md` 获取模型与ONNX Runtime安装包，下载并校验文件哈希
4. 在PYNQ中解压推理库，配置运行环境
5. 运行 `src/software` 下主程序，启动目标检测

## ✅ 文件完整性校验
项目配套 SHA256SUMS.txt，下载资源后可校验哈希，防止文件损坏。



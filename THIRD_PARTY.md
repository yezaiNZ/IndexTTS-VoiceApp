# 上游组件与许可说明

## 前端发行包（v0.4.6 起）

前端使用 Qt / PySide6-Essentials / Shiboken6 6.11.2，版权归 The Qt Company Ltd.
及其贡献者所有。使用其适用的 LGPLv3 授权部分；完整 LGPL/GPL 许可文本、
Qt Base 与 PySide/Shiboken 的原始对应源码及归属文件随 `third-party/` 提供。
组件没有被本项目修改，采用允许用户替换的共享库（onedir）分发。
用户为调试第三方库修改而必要的反向工程、修改及重新链接权利不受额外限制。

构建器不收集未使用的 Qt Virtual Keyboard、Qt PDF、Qt Quick/QML 等组件。
完整说明见发行包 `third-party/README.md`，入口位于主界面和首次准备窗口的
“第三方许可”。源码模式下的生成规则见 `packaging/THIRD_PARTY_SOURCES.md`。
Python、PyInstaller bootloader、requests 依赖及 uv 的版本与许可分别记录在
发行包 `third-party/inventory.json` 和 `third-party/licenses/`。

以上第三方声明不替代或扩大本项目自有代码的授权范围，也不减少上游许可授予的权利。

Voice App 自动获取下列组件，不要求用户手动安装或配置 Python、IndexTTS 或模型。
下载需要用户在向导中同意上游许可；用户应确保所使用参考音频的使用权限。

- IndexTTS 源码与模型：https://github.com/index-tts/index-tts 、https://huggingface.co/IndexTeam/IndexTTS-2.5
- 本项目是独立集成应用，不代表 IndexTTS / bilibili 官方；集成中的修改不受原权利人背书、担保或保证，原权利人不承担相关责任。固定源码的完整英文和中文许可随包保留。
- Meta w2v-BERT 2.0：https://huggingface.co/facebook/w2v-bert-2.0
- FunASR CAMPPlus：https://huggingface.co/funasr/campplus
- NVIDIA BigVGAN：https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x
- Python、PyTorch、Qt/PySide6、uv 保留各自上游许可。Qt 随前端发布；其余按需下载。
- v0.4.7 的后端音频导入使用 SoundFile 0.13.1（BSD-3-Clause）及其上游 libsndfile
  （LGPL）依赖；不将它们的授权改为本项目自有代码授权。它们按需安装到后端推理环境，
  不作为 Qt 前端库新增打入发行包。来源与许可：https://python-soundfile.readthedocs.io/ 、https://github.com/libsndfile/libsndfile 。

## VB-Audio VB-CABLE

安装包附带的是 VB-Audio 原始、未修改的 `VBCABLE_Driver_Pack45.zip`。
仅在用户选择安装时调用官方安装器，由 Windows 请求管理员许可。
不修改系统默认设备、不导入受信任证书，也不自动重启电脑。

VB-CABLE 来自 VB-Audio：https://vb-audio.com/Cable/ 。
它是 Donationware，欢迎用户自愿支持作者或购买许可：
https://vb-audio.com/Services/licensing.htm 、https://shop.vb-audio.com/ 。
商业/机构分发应遵守 VB-Audio 的相应许可要求；本项目没有获得 A+B、C+D 的分发权限，亦不包含它们。

## 下载校验

全部必需模型文件在 `voice_app/model_manifest.py` 中固定大小和 SHA-256。
ModelScope 备用仓库与 HF 镜像只是下载来源；最终接受的内容必须与固定清单一致。
不执行远程模型仓库代码。可选 Qwen 情绪文字模型不属于默认推理功能，不会下载。

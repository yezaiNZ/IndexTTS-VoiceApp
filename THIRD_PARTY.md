# 上游组件与许可说明

Voice App 自动获取下列组件，不要求用户手动安装或配置 Python、IndexTTS 或模型。
下载需要用户在向导中同意上游许可；用户应确保所使用参考音频的使用权限。

- IndexTTS 源码与模型：https://github.com/index-tts/index-tts 、https://huggingface.co/IndexTeam/IndexTTS-2.5
- Meta w2v-BERT 2.0：https://huggingface.co/facebook/w2v-bert-2.0
- FunASR CAMPPlus：https://huggingface.co/funasr/campplus
- NVIDIA BigVGAN：https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x
- Python、PyTorch、Qt/PySide6、uv 保留各自上游许可。Qt 随前端发布；其余按需下载。

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

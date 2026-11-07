# 第三方组件、许可与对应源码

本程序使用 Qt 6.11.2、PySide6-Essentials / Shiboken6 6.11.2。
这些组件的版权归 The Qt Company Ltd. 及其贡献者所有。本发布包使用其适用的
LGPLv3 授权部分，不将商业授权文本解释为向用户提供商业许可证。

## 随包提供的材料

- `licenses/qtbase/`：Qt Base 的完整许可证文本和上游第三方归属文件。
- `licenses/pyside-shiboken/`：Qt for Python / Shiboken 上游许可与归属文件。
- `sources/qtbase-everywhere-src-6.11.2.tar.xz`：与随包 Qt 库版本对应的原始源码。
- `sources/pyside-setup-everywhere-src-6.11.2.tar.xz`：PySide/Shiboken 原始源码及构建脚本。
- `licenses/` 中其余目录：Python、PyInstaller bootloader、requests 及其依赖、uv、IndexTTS 的许可材料。
- `inventory.json`：组件版本、来源、源码压缩包大小及 SHA-256。
- `files.sha256.json`：本目录材料的完整性校验记录。

源码无需另行向作者申请，已包含在本安装版及便携版内。正常使用应用不需要解压或编译源码。
Qt/PySide/Shiboken 源码没有经过本项目修改；原始源码中的构建说明和许可证均保留。
若重新构建，请使用对应版本的源码、Windows x64 构建工具，并遵循上游构建说明。
与官方 wheel 的二进制逐字节重现不是本项目作出的保证。

## 替换与调试共享库

此软件采用 PyInstaller onedir 形式，Qt 和 PySide/Shiboken 以共享库形式放在
程序目录的 `_internal/PySide6`、`_internal/shiboken6` 等位置。
在遵守相关许可证并自行备份的前提下，用户可以修改、重建并替换这些库；
应用不进行阻止替换的签名或哈希锁定，也不限制为调试这些库修改而必要的反向工程。
请使用相同架构和兼容 ABI；替换失败时可从原安装包恢复。
这里的文件校验用于发现下载损坏，不是限制用户依法修改第三方库的技术措施。

打包时只保留本 Widgets 桌面应用需要的 Qt 库和有限的 Windows/图像插件。
不携带 Qt Virtual Keyboard、Qt PDF、Qt Quick/QML 等未使用模块的运行库与插件。
原始源码归档及归属文件保留上游完整内容，可能包含其它示例和模块；这不表示它们
已被编译或链接进本应用。不同材料保持各自原有许可，不把全部第三方内容统一改标为 LGPL。
这份第三方许可说明不替代或扩大本项目自有代码的授权范围，也不会减少上游授予用户的权利。

## IndexTTS 与 VB-CABLE

IndexTTS 固定版本的完整英文和中文许可见 `licenses/indextts/`，源码原始压缩包另在
程序目录的 `engine-source/`。模型并未打入发布包；下载与使用模型仍须遵守对应上游条款。
本项目是独立集成应用，不代表 IndexTTS / bilibili 官方，也未获其背书或性能保证。

VB-Audio VB-CABLE 原始安装归档位于程序目录的 `drivers/`。
VB-CABLE 是 VB-Audio 的 Donationware，用户可根据使用情况自愿捐赠或购买许可。
来源：https://vb-audio.com/Cable/ 。许可与捐赠说明：https://vb-audio.com/Services/licensing.htm 。
专业/机构使用须核对其相应要求；本项目不包含 A+B、C+D 付费驱动包。

这些材料是随发行包提供的技术与归属说明，不是法律意见或对所有使用场景的合规保证。

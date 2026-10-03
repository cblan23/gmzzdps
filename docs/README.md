# 文档索引

## 当前版本

- [使用指南](usage.md)
- [常见问题](../使用问题大全.md)
- [Windows 构建与发布](build.md)
- [v0.3.5 发布验收](release-v0.3.5.md)
- [v0.3.5 更新日志](../release-notes-v0.3.5.txt)

## 技术与历史记录

其他带日期的文档描述当时的实现、实验或问题，测试数量和配置选项可能已变化。当前用户操作以使用指南为准，当前采集入口以 `capture_backend.py` 和 `windows_capture_process.py` 为准。

历史 `npcap_*` 模块还承载共享协议解码。不要因为模块名或旧文档要求当前用户安装 Npcap，也不要将旧实验结果当成当前版本完整实战验收。

当前主窗口使用 `main_hud.py` / `main_hud_artwork.py`，设置页在 `dps_meter.pyw` 中。设置界面改版设计尚未落入本次程序包。

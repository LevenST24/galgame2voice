# 文档导航

## 使用程序

- [简明使用说明](../使用说明.md)：启动入口与本地文件夹用途。
- [Windows 便携版](WINDOWS_PORTABLE.md)：安装、更新、备份和故障排查。
- [项目 README](../README.md)：功能、源码安装与角色包格式。
- [安全说明](../SECURITY.md)：鉴权、凭据与网络访问边界。
- [更新记录](../CHANGELOG.md)。

## 开发与测试

以下命令从项目根目录执行。开发缓存集中在 `.local/cache/`，临时测试文件可放在 `.local/tests/`：

```powershell
python -m pytest tests -q --basetemp=.local/tests
python -m ruff check galgame2voice scripts
cd frontend
npm test
npm run deploy
```

- [项目架构与接口](development/PROJECT.md)。
- [测试规范](development/TEST_INFRA.md)。
- [测试运行说明](development/TEST_READY.md)。
- [立绘坐标与导出](development/PORTRAIT_EXPORT.md)。

根目录保留 Python、Git、Docker 和启动脚本的标准配置位置，避免改变启动、安装及 CI 的行为。

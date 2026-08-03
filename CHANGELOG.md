# Changelog

## 0.2.0 - 2026-07-28

- 增加应用名搜索、有界 JSON 分页、精确包名 alias 解析和歧义结果显式选择。
- 增加 `search`、`list`、`download` 三个子命令，同时保留 URL 直调兼容入口。
- 从来源页面解析 versionCode、完整更新时间、size、MD5、CRC32 和 minSDK。
- 增加有界重试/并发、HTTPS/host 门禁、字节上限和原子写入。
- 校验来源 size/MD5、本地 SHA-256、ZIP 结构、Android manifest，以及可选 `aapt2` package/versionCode 一致性。
- 增加脱敏 evidence manifest、部分失败退出码和离线单元测试。

# wandoujia-downloader

豌豆荚历史版本 APK 获取工具。支持按应用名、包名、App ID 或豌豆荚链接查找应用，列出历史版本，并在下载 APK 时做基础完整性校验。

推荐流程：

```text
search -> list -> download
```

## 功能

- 支持按应用名搜索，也支持包名精确解析，例如 `com.tencent.mm`。
- 支持 `/apps/<id>`、`/history`、`/history_yYYYY`、`/history_vNNNNN` 等豌豆荚链接。
- 支持 `search`、`list`、`download` 三个子命令，同时保留旧的 URL 直调下载方式。
- 下载时校验来源页面声明的 size / MD5，并额外计算本地 SHA-256。
- 校验 APK 是否为 ZIP 且包含 `AndroidManifest.xml`。
- 可选传入 `aapt2`，复核 APK 内部 package、versionName 和 versionCode。
- 输出 `wandoujia-manifest.json`，记录来源、哈希、版本和保存状态。
- 默认只允许 HTTPS 豌豆荚页面，以及 `pp.cn` / `25pp.com` APK CDN；如果 CDN 变化，需要显式增加 `--allow-download-host`。

来源完整性只表示下载文件与豌豆荚页面声明一致，不等于发布者签名真实性。需要真实性判断时，请另外比对 APK 签名证书。

## 安装

Python 3.10+：

```bash
python -m pip install -e .
wandoujia-downloader --help
```

也可以直接运行 checkout：

```bash
python wandoujia_downloader.py --help
```

依赖：

```bash
python -m pip install aiohttp
```

## 搜索应用

```bash
python wandoujia_downloader.py search "微信"
python wandoujia_downloader.py search "com.tencent.mm" --json
```

搜索结果包含 App ID、包名、当前 versionName / versionCode 和规范 App URL。名称搜索最多读取 10 页搜索分页，并按 App ID 去重。名称不唯一时，后续 `list` / `download` 会要求使用 `--select N` 明确选择。

## 列出历史版本

```bash
python wandoujia_downloader.py list "com.tencent.mm" --latest --json
python wandoujia_downloader.py list 596157 --year 2025 --limit 10
python wandoujia_downloader.py list 596157 --version-code 3120
```

默认输出会脱敏 download URL 中的 `did` / token / sign 类查询值，同时保留 size、MD5、CRC32、minSDK 等可复核字段。只有明确使用 `--show-download-urls` 才输出完整 URL。

## 下载并验证

```bash
python wandoujia_downloader.py download "com.tencent.mm" \
  --version-code 3120 \
  --out-dir ./apks \
  --manifest ./apks/wandoujia-manifest.json
```

可选传入 `aapt2` 做 APK 元数据复核：

```bash
python wandoujia_downloader.py download 596157 \
  --limit 5 \
  --out-dir ./apks \
  --manifest ./apks/wandoujia-manifest.json \
  --aapt2 /path/to/aapt2
```

输出文件名包含包名、versionName、versionCode 和完整发布日期，例如：

```text
com.tencent.mm-8.0.74-3120-20260612.apk
```

写入流程为同目录 `.part` -> 完整性校验 -> 原子替换。目标已存在且未指定 `--overwrite` 时，会重新校验现有文件；不匹配会失败，不会生成含糊的 `__1` 副本。

### 批量下载

不加 `--latest`、`--limit`、`--version` 或 `--version-code` 会处理页面内全部可解析版本，可能产生大量请求与磁盘占用。自动化任务建议先执行 `list`，再显式限定范围。

### Manifest

默认写入 `OUT_DIR/wandoujia-manifest.json`，schema 为 `wandoujia-downloader.manifest.v1`，定义见 [`schemas/manifest-v1.schema.json`](schemas/manifest-v1.schema.json)。每个 artifact 包含：

- 来源 detail URL、脱敏 download URL 和完整 URL SHA-256；
- 来源声明 size / MD5 / CRC32 / minSDK；
- 本地 size / MD5 / SHA-256；
- ZIP / manifest 校验状态；
- 页面元数据或 `aapt2` 实测 package、versionName、versionCode；
- `saved | existing | failed` 状态与失败原因。

退出码：

| 退出码 | 含义 |
|---:|---|
| `0` | 所有请求均已保存或命中通过校验的现有文件 |
| `2` | 输入、解析、网络或全部下载失败 |
| `3` | 批量任务部分成功、部分失败 |

## 兼容旧调用

原来的 URL 直调仍映射到 `download`：

```bash
python wandoujia_downloader.py \
  "https://www.wandoujia.com/apps/596157/history" \
  --dry-run --limit 5
```

## 开发

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile wandoujia_downloader.py src/wandoujia_downloader/*.py
```

单元测试只使用合成 HTML / APK，不依赖豌豆荚在线状态。真实网络 smoke 建议只取少量版本，并把 APK 输出到临时或手工指定目录。

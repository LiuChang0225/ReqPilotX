# 合成 PDF：真实 HTTP 工作台检查

`scripts/smoke_workbench.py` 在一次性 PostgreSQL 和对象目录中运行真实应用 API。它不调用模型、不启动 worker，也不连接已有数据库。输入由合成 fixture 当次生成，语义决定由检查脚本明确构造，不能作为模型提取质量或客户验收证据。

## 运行

先按[应用说明](../../prototype/README.md)安装 Python 依赖、构建前端，并在本机准备 PostgreSQL 程序。脚本从 `PATH` 的 `pg_ctl` 找到同目录 `initdb`、`createdb`、`postgres`，也可以显式指定目录。

```bash
prototype/.venv/bin/python scripts/smoke_workbench.py
prototype/.venv/bin/python scripts/smoke_workbench.py --postgres-bin /absolute/path/to/postgresql/bin
```

默认创建私有临时结果目录，绑定随机 loopback 端口，应用正式 migrations；结束后停止自有 API/数据库，保留回执、日志、合成输入及导出用于核对。`--output /absolute/new/output` 可指定尚不存在的目录，不覆盖已有目录。

需要浏览器复核时：

```bash
prototype/.venv/bin/python scripts/smoke_workbench.py --keep-server
```

只有 PASS 才保留服务，输出包含精确工作和版本的 `workbench_url`。完成浏览器检查后，使用实际返回的结果目录停止：

```bash
prototype/.venv/bin/python scripts/smoke_workbench.py --stop-output /absolute/result/directory
```

停止前核对 API 的实际命令与端口，以及 PostgreSQL owner 标记和精确 PGDATA；身份不匹配时拒绝。停止不删除回执或数据目录，不能用这个入口管理已有开发服务。

## 检查范围与证据

1. 新数据库为空，正式 SQL migrations 全部应用；真实 HTTP 创建项目、上传合成 PDF，下载字节与输入一致。
2. 插件共用编译器当次生成 Map/Items；通过真实 HTTP 同步 Map、部分条目和完整条目，共三版，完整结果为八个 SRC、七个 REQ。
3. HTTP 保存绑定具体条目与版本的反馈，重复 command 不重复插入；共用编译器增加剩余范围，保留旧条目身份，新版保存反馈处理说明。
4. 旧快照保持不变，过期写入返回 409；当前快照、原文读取、任务列表和详情可以通过公开 API 读取。
5. 通过真实 HTTP 下载 Excel，检查响应版本、交付哈希和工作簿内嵌快照哈希。
6. 真正停止并重启 API，读取同一 PostgreSQL/对象目录，反馈、完整版本历史、原件和快照保持不变，重新导出仍绑定同版内容。
7. 工作台 HTML 入口真实返回；浏览器渲染和交互需要另行检查，不由 HTTP 200 代替。

`receipt.json` 记录每个请求的 method/path/status/响应哈希、来源/基线/版本、迁移与两个 Excel 文件哈希；`matrix-snapshot.json`、`final-state.json`、`reader.json`、实际 XLSX 与服务日志保存在同一结果目录。

哈希口径不同：`delivery_sha256` 不包含它自身；Excel `SnapshotSHA256` 对完整 canonical snapshot（含 `delivery_sha256`）签名。两者须各自符合合同；导出时间或 ZIP 元数据变化可使两个 XLSX 字节哈希不同，不表示业务快照不同。

此检查证明合成来源的真实本地应用/插件传输、版本和导出恢复链路。它不证明模型语义、所有 PDF/Office、Pair 宿主、云部署或 PostgreSQL 崩溃恢复全部通过。SQLite 模式不具备正式 durable runtime，本检查因此使用自建 PostgreSQL，不替换业务服务或绕过运行时守卫。

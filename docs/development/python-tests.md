# Python 隔离测试

默认后端和插件测试使用 Python 3.13。应用依赖保持在 `prototype/requirements.txt`；
测试、MCP stdio 与本地 OCR 网格依赖在 `prototype/requirements-test.txt`。
当前 FastAPI/Starlette 固定版本与 MCP 的 Python 3.14 依赖条件不同，因此不要用 Python 3.14 重建这个基线。

## 从干净 checkout 重建

从仓库根执行；需要已安装 `uv` 和 Python 3.13：

```bash
uv venv --python python3.13 prototype/.venv
# macOS 使用此锁；Linux x86_64 改用 requirements-test-linux-py313.lock。
uv pip sync --python prototype/.venv/bin/python prototype/requirements-test-macos-py313.lock
uv pip check --python prototype/.venv/bin/python
npm --prefix prototype/frontend ci
npm --prefix prototype/frontend run build
prototype/.venv/bin/python scripts/test_python.py all
```

锁文件固定当前平台的全部间接依赖；不把 macOS 的 PyObjC 锁用于 Linux。
全量后端测试会验证 API 的前端静态资源挂载和 Vite manifest，因此需要先构建前端；
只运行不涉及前端的聚焦测试时可省略 Node 步骤。
安装会包含应用现有的 Torch 等 Python 依赖，不下载模型权重。
需要本机 OCR 的测试在未安装 `tesseract` 时明确跳过；CI 安装该二进制。

## 聚焦运行与隔离边界

```bash
prototype/.venv/bin/python scripts/test_python.py backend --pattern 'test_runtime_isolation_contract.py'
prototype/.venv/bin/python scripts/test_python.py plugin
```

入口移除继承的 ReqPilot/ReqPilotX（含私有评测根）、Azure、OpenAI、百炼/阿里云配置，创建临时存储目录，
同时移除测试子进程继承的 HTTP/SOCKS proxy 变量，避免离线 SDK 构造受宿主代理及可选依赖影响；实际 socket 阻断保持启用。
强制测试部署、SQLite 测试数据库、本地临时对象存储及禁用 Agent runtime。
它关闭 Hugging Face 在线下载，并在测试进程中阻断实际 socket 连接；
ASGI 内存 transport、mock provider 与 MCP stdio 仍可运行。
入口结束时自动清理临时存储。测试日志可写入已忽略的 `output/`。

真实 PostgreSQL、真实模型、私有语料测试的 opt-in 不从父环境继承。
这些验证应走各自独立的显式入口及一次性目标，不能用本命令宣称真实服务已验收。
网络 audit hook 只覆盖测试进程；测试自行启动的子进程继承隔离配置与离线变量，
但不继承 Python audit hook。MCP 使用本仓脚本和标准输入输出。

## 更新锁文件

仅在明确更新依赖时运行，然后重新验证：

```bash
uv pip compile --python-version 3.13 prototype/requirements-test.txt -o prototype/requirements-test-macos-py313.lock
uv pip compile --python-version 3.13 --python-platform x86_64-unknown-linux-gnu prototype/requirements-test.txt -o prototype/requirements-test-linux-py313.lock
```

macOS 锁在 macOS 上生成；Linux 锁可从 macOS 交叉解析。

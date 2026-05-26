# Aliyun 本地 Qdrant POC 清单

更新时间：2026-04-02 07:36 CST  
适用仓库：`/Users/liuchang/Documents/ReqPilotX`

## 1. 适用范围

- 目标是让 `Aliyun + knowledge-backed path` 在非公司网络下可运行。
- 这条路径是 `aliyun-local-poc`：`DashScope + local Qdrant`。
- Azure 仍是默认主线；这份清单不是 Azure 的替代方案，也不代表正式 Aliyun 生产检索后端已经定案。

## 2. 运行前提

- 已安装 `prototype/requirements.txt` 中依赖。
- 本地有可访问的 DashScope key。
- 本地有一个正在运行的 Qdrant 服务。
- 默认 profile 仍建议保留为 Azure；只有明确需要时才把运行 profile 切到 `aliyun`。

## 2.1 首页配置入口

- 当前首页已新增 “AI 路径设置” 卡片，位置在知识库上传之前。
- 如果选择 `Aliyun`：
  - 需要填写 runtime DashScope API key
  - 默认模型已预填：`qwen3.5-plus` + `text-embedding-v4`
  - 默认 Qdrant 配置已预填：`http://localhost:6333` + `reqpilot-knowledge-aliyun-local`
- 如果选择 `Azure`：
  - 当前本地推荐仍是 Azure CLI / RBAC
  - 页面提供的是 RBAC 检查入口；浏览器交互登录只作为 fallback，不是默认主路径
- 显式选择的 default profile 会用于知识库上传；如果 Aliyun 没配好，上传会被前置拦住，不会自动回退到 Azure

## 3. 本地 Qdrant 启动

无 Docker 方式优先：

1. 准备可执行的 `qdrant` binary。
2. 在本机直接运行该 binary，保持默认端口 `6333/6334`。
3. 用 `http://localhost:6333/collections` 做连通性确认。

Docker 仅作为可选方式：

```bash
docker run --rm -p 6333:6333 -p 6334:6334 qdrant/qdrant
```

## 4. 必需环境变量

最小必需：

```bash
export REQPILOT_ALIYUN_RETRIEVAL_BACKEND=qdrant
export REQPILOT_ALIYUN_API_KEY=...
export REQPILOT_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export REQPILOT_ALIYUN_CHAT_MODEL=qwen3.5-plus
export REQPILOT_ALIYUN_EMBEDDING_MODEL=text-embedding-v4
export REQPILOT_ALIYUN_QDRANT_URL=http://localhost:6333
export REQPILOT_ALIYUN_QDRANT_COLLECTION=reqpilot-knowledge-aliyun-local
```

可选：

```bash
export REQPILOT_ALIYUN_QDRANT_API_KEY=...
```

通用 fallback 仍有效：

- `REQPILOT_QDRANT_URL`
- `REQPILOT_QDRANT_API_KEY`
- `REQPILOT_QDRANT_COLLECTION`
- `QDRANT_URL`
- `QDRANT_API_KEY`

## 5. 兼容性边界

- Azure 仍是仓库默认主线；但如果用户在首页显式切到 Aliyun，default profile 会按用户选择生效。
- 知识库上传时默认只同步 default profile；当前默认就是 Azure。
- Aliyun 的 knowledge sync 在用户显式以 `profile_id=aliyun` 跑分析时才懒触发。
- Aliyun Qdrant 缺配或不可达时，只会反映在 `/api/readiness` 的 `checks.aliyun`，不会拖垮 Azure 已经 runnable 的整体 readiness。

## 6. Smoke 命令

在 `prototype/` 下运行：

```bash
.venv/bin/python scripts/aliyun_local_rag_smoke.py \
  --sor-file examples/demo_sor.txt \
  --knowledge-file examples/demo_knowledge.txt
```

默认行为：

- 使用隔离存储根：`prototype/storage/smoke/aliyun_local_rag`
- 运行前先探测 Qdrant `/collections`
- 分析固定走 `profile_id=aliyun`
- 输出 summary 到 `prototype/eval/aliyun_local_poc/latest/`

## 7. 常见问题

- `Qdrant preflight failed`：本地 Qdrant 没启动、URL 填错，或端口未暴露。
- `ALIYUN_QDRANT_UNREACHABLE`：`/api/readiness` 已读到 URL，但服务不可达；先确认本机 `6333` 可访问。
- collection 维度冲突：已有 collection 的向量维度与当前 embedding model 不一致；请换新 collection 名或清理旧 collection。
- DashScope key 缺失：补 `REQPILOT_ALIYUN_API_KEY` 或 `REQPILOT_DASHSCOPE_API_KEY`。
- 误切换到 Aliyun：检查前端 profile selector，或确认是否手动设置了 `REQPILOT_AI_DEFAULT_PROFILE=aliyun`。

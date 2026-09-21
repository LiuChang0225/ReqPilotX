# 原生任务能力包：构建、登记与切换

原生任务的发行默认是 `registered-release`。宿主先准备完整的 R1 wheel
插件包并登记其精确 `bundle_sha256`；创建任务时，服务校验登记包、复制到任务私有
`plugin/`、再校验副本，随后才写任务记录并入队。任务、队列 payload 和后续恢复使用
同一个完整插件身份；`engine_release.core_identity` 只是组成信息，不能替代插件身份。

## 准备并登记发行包

从仓库根执行，输出目录和 registry 必须是宿主管理的私有路径：

```bash
prototype/.venv/bin/python plugins/reqpilot/scripts/build_engine.py \
  --repo . --output /private/releases/reqpilotx-engine

prototype/.venv/bin/python prototype/scripts/prepare_native_loop_capability.py \
  build-register \
  --repo . \
  --engine-wheel /private/releases/reqpilotx-engine/reqpilotx_engine-*.whl \
  --bundle-output /private/releases/reqpilotx-plugin-v1 \
  --registry-root /private/reqpilotx/native-loop-capabilities
```

命令返回登记路径、完整 `bundle_sha256` 和三项宿主配置。已有完整包可用
`register --bundle ... --registry-root ... --expected-bundle-sha256 ...` 登记；
`expected-bundle-sha256` 用于把外部发布记录中的预期身份与实际文件再核对。登记和
任务复制都不会覆盖现有目录，失败的 staging 会清理。

启动 API 与 worker 前，把返回值写入同一宿主配置：

```bash
export REQPILOT_NATIVE_CAPABILITY_MODE=registered-release
export REQPILOT_NATIVE_CAPABILITY_REGISTRY=/private/reqpilotx/native-loop-capabilities
export REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256=<exact-64-character-bundle-sha256>
```

可在启动前复核当前选择：

```bash
prototype/.venv/bin/python prototype/scripts/prepare_native_loop_capability.py \
  inspect \
  --registry-root "$REQPILOT_NATIVE_CAPABILITY_REGISTRY" \
  --bundle-sha256 "$REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256"
```

`inspect` 要求完整 `ENGINE_WHEEL` 插件、完整声明清单、受控路径、实际文件 SHA、
引擎来源和无需运行期 checkout 的发行回执全部成立。缺包、开发包、身份错误、额外或
缺失文件、symlink/路径逃逸都会拒绝；不会改用当前 checkout，也不会安装或下载依赖。

## 默认切换与回滚

先构建并登记 B，再把 `REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256` 从 A 改为 B 并按
既有部署方式重启配置消费者。B 只用于之后创建的新任务；已创建、重试、排队或恢复的
A 任务继续验证和使用自己的 A 副本。相同创建命令仍返回原任务和 A。

回滚只需把默认 SHA 改回仍在 registry 中的 A 并重启配置消费者。不要覆盖任务内
`plugin/`、不要删除旧 registry 包，也不要改写任务 metadata。旧任务包丢失或被篡改时，
执行会停止；修复应从同一精确身份的保留产物显式恢复，不能拿当前默认包或源码重建冒充。
已有显式 capability upgrade 仍通过原授权、终态检查和 epoch 接口完成，保留旧包。

## 显式开发模式

只有本地开发需要运行期源码构建时才设置：

```bash
export REQPILOT_NATIVE_CAPABILITY_MODE=development-source
export REQPILOT_DEVELOPMENT_ROOT=/absolute/path/to/ReqPilotX
```

`local` 本身不会启用该模式，cloud 明确拒绝该模式。`REQPILOT_AGENT_RUNTIME=none`
的只读/测试服务无需能力包即可启动；真正发起原生任务时才检查发行配置。

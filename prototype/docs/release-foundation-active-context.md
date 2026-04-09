# Release Foundation Active Context

更新时间：2026-04-09 23:23 CST  
分支：`codex/release-foundation`

## 分支目的

- 收口试运行前的 P0 稳定性问题
- 接入 Entra 登录与内部试运行反馈骨架
- 让控制塔和真实运行底座对齐

## 当前关注

- control tower / trackctl / skill 已就位，等待该支线真正开工
- health / readiness / smoke 基线
- 账号系统接口与会话状态
- 反馈采集最小闭环

## 当前 blocker

- 对应 worktree 还没创建，控制塔当前会把这条 active track 保持在 `TODO / ON_PLAN / not_started`
- 上传长任务、readiness 语义、SQLite 故障路径仍是既有技术债
- 认证与反馈还没有正式落地

## 下一步

- 创建 `codex/release-foundation` worktree
- 开工即运行 `python prototype/scripts/trackctl.py start --track release-foundation`
- 先推进 P0 稳定性、auth 和 feedback 骨架

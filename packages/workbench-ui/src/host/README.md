# 共享只读工作面板

此入口复用正式前端的`usePluginWorkbench`、`PluginWorkbenchContent`和原文／台账组件。它不创建React root、不读取或重写宿主路由、不启动Agent、不创建第二份业务数据。当前首切片限定`plugin-*`便携工作，实际验证范围为PDF；不是已兼容全部原生工作或已安装完整DSH应用。

## 产物与加载

在`prototype/frontend`执行`npm run build:panel`：

- `dist-panel/reqpilotx-readonly.js`：ESM入口。
- `dist-panel/reqpilotx-readonly.cjs`：CommonJS入口，供DSH包装成其lazy-CJS工厂。
- `dist-panel/reqpilotx-readonly.css`：必须加载的业务样式；由同一`styles/index.css`／`workbench.css`生成作用域版本，不维护第二份样式源码。

构建external配置为`react`、`react-dom`、`react/jsx-runtime`、`react/jsx-dev-runtime`、`react-dom/client`；当前产物实际引用前三个。所有引用必须解析到宿主提供的同一React18.3.1／ReactDOM18.3.1实例，库不自带运行时。不要执行主应用的`main.tsx`或把整个`App`挂入宿主。

公开组件为`ReqPilotXReadonlyPanel`，公开类型和帮助函数从`src/host/index.ts`导出。完整TypeScript合同以`readonlyHost.ts`和`ReqPilotXReadonlyPanelProps`为准。产物不自注册Pair面板，DSH Host／Client注册由集成包装负责。

```tsx
<ReqPilotXReadonlyPanel
  documentName={registeredDocument.file_name}
  selection={selection}
  host={hostAdapter}
  theme={hostTheme}
  active={isActivePanel}
/>
```

`selection`必须明确`projectId`、`documentId`、`sourceSha256`、`workRef`；`revisionId`可指定历史版，没有成果的明确工作可省略。首次读到成果时会通过`onSelectionChange`返回实际查看版。`view`为`structure | items | analysis | export`。选择只有Map的版本会回到结构视图。库不从通用文档入口挑选latest工作。

## HostAdapter的职责

| 字段 | 合同 |
|---|---|
| `id`／`client.id` | 稳定的宿主／数据连接身份；更换来源服务时同步更换client.id，使旧读状态失效 |
| `client.request` | 与共同request签名相同的实例级读取函数。`createReadonlyHttpClient`复用共同JSON／错误解析，阻止GET／HEAD之外的请求。业务写操作另在provider层再次禁用 |
| `resourceUrl(path)` | 解析原件、裁剪、知识引用及导出的URL，保留路径／query里的准确来源和版本；不要把每个路径替换成固定latest地址 |
| `pdfViewerUrl(sourceUrl,page,embedded)` | 返回可用PDF页面地址和asset／worker通道。跨origin独立viewer可传`embedded=1&parentOrigin=<宿主origin>`，必须为准确http(s) origin；PDF继续同时检查origin与实际父窗口 |
| `sessionBinding` | 宿主实例、workspace、session与业务project／document／source／work的明确绑定。持久化由DSH Host或宿主存储负责，组件只校验和消费，不自行建立隐藏绑定 |
| `openSession(binding,context)` | 仅用户点击时调用，打开已绑定会话并准备上下文，不自动发送prompt。选中上下文包含当前revision、node、sourceItem及可选derivedUnit；未就绪时拒绝把单条范围扩大成整文 |
| `execution.getSnapshot/subscribe` | 当前绑定工作的真实运行观察。snapshot对象在没有事件时保持稳定；适配器负责当前run及连接状态。`SUCCEEDED`只表示宿主本轮结束，不表示提取／语义／人工验收完成 |
| `subscribeWorkUpdates` | 只发真实业务成果保存事件，携带相同来源／work和新revision。匹配事件刷新成果状态；逐token进度不要触发此事件。当前查看的历史版不自动跳最新 |
| `onSelectionChange` | 宿主保存面板的工作／版本／视图，卸载重挂时通过selection恢复。组件不写宿主history，也不在关闭时取消会话 |

没有会话绑定时仍可读已有成果，讨论按钮禁用；没有运行观察则显示未报告，不能用目录中已有Map／Items推断本轮Agent完成了提取。断连显示断连与最后报告时间，保留已读取成果。

若未提供业务成果事件订阅，active面板每5秒同步已保存状态；`active=false`停用该轮询。事件订阅在卸载时清理。只读模式隐藏编辑／确认入口，保留可读取的历史信息和服务声明可用的导出。

## 验证与限制

`panel-preview.html`及`vite.panel-preview.config.ts`是开发期宿主兼容验证页，不是Pair产品界面。它加载真正构建的库，React18fixture与临时类型检查配置放在忽略提交的`prototype/runtime-data/ui-pair-panel-20260914/`。主Web应用保留React19依赖。

实际Pair的sidebar/main注册、Session持久绑定与真实Pair事件转发仍由DSH集成任务完成；此面板的契约／模拟事件通过不等于这些宿主动作已部署。完整项目管理、上传、阅读问答和业务写操作属于后续接入切片。

## 共源升级合同

项目/文档业务客户端在`shared/api/projectDocuments.ts`，独立App的`api`与宿主导出共同使用。Pair通过`createProjectDocumentClient`注入transport，禁止在适配层重写create/import请求字段。`requestWithTransport`继续复用错误/JSON解析；上传进度由真实upload transport提供。

`contract.ts`定义共享导出面的major/minor；构建检查实际exports并生成`dist-panel/host-contract.json`。消费端声明所需major与exports，兼容新增不会强迫适配器改代码。contract版本、能力包版本和具体成果revision分别记录，不能互相替代。

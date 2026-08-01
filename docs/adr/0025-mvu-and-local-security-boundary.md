# ADR-0025：MVU 校验与本地安全边界

- **状态**：Accepted（策略与威胁模型已锁定；HTTP/浏览器 enforcement 仍需实现）
- **日期**：2026-08-02
- **关联**：Wayfinder #18、ADR-0006、ADR-0011、ADR-0015、ADR-0021、ADR-0024、`CONTEXT.md`

## Context

AIRP 是本地应用，但它同时处理不可信的角色卡内容、模型输出、浏览器请求和 Provider
响应。当前运行时有一些保护：提交门禁已经使用独立的严格 MVU validator；静态文件路径
有 root confinement；`LocalSecretStore` 使用 0700/0600 文件权限并在输出中 redaction。
但仍存在四个实际暴露面：

1. 兼容浏览器路径需要保留对新 MVU 字段的宽松行为，而正式 commit 不能允许模型任意
   改变状态结构；
2. 导入后的角色卡 beautify/template 会被放进主页面并重新执行 `<script>`，与导入阶段
   的 Node `vm` 隔离不是同一个安全边界；
3. CLI 当前把 HTTP server 绑定到 `0.0.0.0`，响应的 CORS 是 `*`，Studio、存档和
   回合命令没有统一的请求授权；
4. Markdown/HTML、外部 URL、卡片资源和本地文件操作必须区分“展示数据”和“可执行能力”。

威胁模型覆盖：同机任意网页、局域网其它设备（当用户显式暴露服务时）、恶意或损坏的
   导入卡片、恶意模型输出和伪造的浏览器请求。不覆盖已经拥有当前用户文件读写权限的
   本机进程或 root；Provider 本身的密钥泄漏也不由 UI 直接解决。

## Decision

### 1. MVU 的权威提交边界

- Runtime 在 `base_revision` 上冻结 schema；模型输出的 MVU command 在 commit 前必须
  通过严格 validator 和 dry-run，失败时不写 revision、commit、projection 或事件。
- 路径不存在时默认拒绝。只有角色卡 schema 在该对象上明确声明 `*` wildcard，才允许
  新增动态子键；不引入隐式 `custom.*` 前缀，也不根据运行中偶然出现的字段扩大 schema。
- wildcard 下仍执行操作类型、父容器和值类型检查；接受的动态路径、旧值、新值和来源
  进入审计差分。
- 旧浏览器/兼容 handler 可以继续使用宽松 validator 读取和展示卡片，但它不是 Runtime
  的权威写入口。任何进入 Session revision 的写入都回到上述严格边界。

### 2. 角色卡内容与脚本

角色卡是数据，不是 AIRP 的代码安装包。

- 默认正文、开场、世界书和 beautify 只经过安全 Markdown/HTML 渲染；禁止
  `<script>`、内联事件属性、`javascript:` URL、`iframe`、`object`、`embed`、表单和
  任意外部脚本/样式加载。允许的表格、换行、粗体等展示能力不能绕过 sanitizer。
- `tavern_helper` 仅在导入阶段的独立 Node sandbox 中执行，用于提取 schema、默认值和
  injection metadata；它不能访问 `require`、`process`、文件系统、网络或 Provider secret，
  且必须有可杀死的 wall-clock/输出大小上限。失败只产生导入诊断 finding，不执行回退脚本。
- 浏览器主页面不重新执行卡片 `<script>` 或外部 `src`。如果未来需要完整酒馆 UI 兼容，
  必须是显式 Project 级兼容模式，运行在隔离 iframe、严格 CSP 和无 parent cookie/secret
  的环境；这不属于当前默认能力。

### 3. Local HTTP、Origin 和授权

- Runtime 默认绑定 `127.0.0.1`。LAN 监听必须由用户显式传入 host，并在 UI/启动输出中
  标为暴露模式。
- 不使用 `Access-Control-Allow-Origin: *`。默认只允许服务自己的同源请求；显式开发
  allowlist 只允许配置过的 Origin，不能把任意 Origin 与 credentials 组合。
- 每个 Runtime 进程生成一次性的内存 capability。所有 `/v1`、兼容 `/api`、SSE 和包含
  projection/Studio 数据的动态资源都需要授权：浏览器用同源 `HttpOnly; SameSite=Strict`
  会话 cookie，非浏览器客户端用 `Authorization: Bearer`。token 不进入 URL、日志、事件、
  Trace 或错误文案。
- 对有 Origin 的请求执行 Origin 校验；跨站请求、缺少授权的请求和不符合方法/内容类型
  的请求在路由业务逻辑之前拒绝。静态路径必须继续 root confinement，资源 ID 必须通过
  稳定字符集校验；HTTP body 使用大小上限和对象 schema，导入接口只接收文档数据，不接收
  任意本地文件路径。

### 4. Provider secret 与外部操作

- API key 只由 `SecretStore` 读取并交给 Provider Adapter；不进入 Provider/Agent/Graph
  definition、Execution Plan、Context Manifest、事件、Trace、SQLite、projection、
  导出或前端响应。
- 本地 phase-one store 保留 0700 目录和 0600 文件、原子替换与 redaction；权限或格式
  检查失败时 Provider 调用失败，不降级为把 key 写入普通配置。
- 角色卡、Worldbook、Markdown、Regex Collection 和模型文本不能自行选择任意本地 URL、
  文件路径或 HTTP endpoint。外部资料访问只能由明确的 Runtime tool/provider capability
  发起，并记录目标、调用结果和取消/超时；禁止把 secret 放在 query string。

### 5. 展示与错误语义

- Markdown 渲染采用文本节点/allowlist 方式，支持 AIRP 已承诺的粗体、换行和表格；任何
  未允许的 HTML 被转为文本或丢弃，不通过 `innerHTML` 直接执行。
- 安全策略、schema、Origin、授权、body 大小和资源路径错误返回稳定的 4xx error code，
  不产生部分 commit；导入兼容性问题继续使用 ADR-0024 的 `degraded`/`failed` 报告，不能
  用“导入成功”掩盖脚本被禁用或资源被跳过。
- 最小可验证威胁测试包括：未知 MVU 路径拒绝、wildcard 路径及类型校验、卡片 script 不
  执行、外部 `src` 被阻止、Origin/CORS 拒绝、未授权 API/SSE 拒绝、静态路径穿越、超大
  body、secret redaction 和 Provider URL 不被卡片内容控制。

## Current implementation status

已有证据：严格 MVU 入口、静态 root confinement、Project/Worldbook ID 校验、
`LocalSecretStore` 的 0700/0600 原子写入和 secret redaction。尚未实现：wildcard 在严格
validator 中的显式放行、默认 loopback host、Origin allowlist、capability auth、动态
资源认证、HTML/script sanitizer，以及从主页面移除卡片脚本重执行。它们是后续实现 ticket，
本 ADR 不把策略写成已完成代码。

## Consequences

- 角色卡兼容从“执行任意作者脚本”收敛为“导入可解释数据并安全展示”；需要完整酒馆 UI
  的用户必须明确承担兼容模式风险。
- 本地服务默认不再对局域网开放，调试/远程浏览需要显式配置并携带授权。
- strict MVU 保护了 Session 事实源，同时通过 schema wildcard 保留卡片声明的动态 NPC、
  计数器等能力。
- 这些限制不决定任意 DAG 或多 Agent 世界模拟语义，也不替代未来系统级凭证存储。

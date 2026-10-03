# YAML 声明式配置与 GitHub 登录

设置环境变量 `ASTRBOT_PROVISIONING_FILE` 指向 UTF-8 YAML 文件，重启后生效。此 fork 的可选配置层仅管理 WebUI 登录和模型提供商，不迁移上游的 JSON 文件或数据库。不设置该环境变量时，保持上游行为。

```yaml
version: 1
auth:
  password_login_enabled: false
  github:
    client_id: !env GITHUB_CLIENT_ID
    client_secret: !env GITHUB_CLIENT_SECRET
    redirect_uri: https://astrbot.example.com/api/v1/auth/github/callback
    allowed_organizations: [your-organization]
    allowed_users: [your-github-login]
providers:
  sources:
    - id: openai
      provider: openai
      type: openai_chat_completion
      provider_type: chat_completion
      enable: true
      key: [!env OPENAI_API_KEY]
      api_base: https://api.openai.com/v1
      timeout: 120
  models:
    - id: openai/gpt-4.1
      provider_source_id: openai
      model: gpt-4.1
      enable: true
```

`providers.sources`、`providers.models` 分别沿用上游 `provider_sources`、`provider` 的条目格式。省略 `providers` 时仍由 WebUI 管理；声明该段时，两个列表**完整替换运行时配置**，空列表表示清空运行时提供商。启用前应导出现有配置并核对，重复 ID、无效模型引用会阻止启动。各提供商的专用选项保持上游语义。默认模型选择、会话覆盖、聊天规则、插件等仍由上游管理。

YAML 每次启动只读取一次，不热加载。由 Kustomize 生成的 ConfigMap 内容变化会触发滚动更新；环境变量 Secret 更新也需要重启。YAML 管理期间，WebUI/API 修改提供商会明确报错，查询和连通性测试仍可使用。原有 JSON 提供商列表保留在磁盘，其他配置照常保存。移除 `providers` 后重启，将恢复原有列表。通过 YAML 注入的提供商密钥不会写回 `cmd_config.json`，但已登录管理员仍可通过现有配置 API 查看它们。

`!env NAME` 将环境变量解析为字符串；变量缺失或为空、YAML 格式错误、重复键、不支持的版本和未知顶层配置字段会阻止启动，避免意外回退。不要将密钥写入 YAML 或提交到仓库。提供商条目保留上游灵活的选项字典。

## GitHub OAuth App

在 GitHub 创建 **OAuth App**，回调地址必须与 `redirect_uri` 完全一致。生产环境要求 HTTPS；本地测试允许 `http://127.0.0.1:6185/api/v1/auth/github/callback`，访问 WebUI 时也使用 `127.0.0.1`。反向代理应保留公开访问地址并转发至 AstrBot，不通过代理身份请求头绕过认证。

GitHub 用户名在 `allowed_users` 中，**或者**在任一 `allowed_organizations` 组织中具有有效成员资格，即可登录；比较时忽略大小写。两个列表都为空时拒绝所有用户，待接受的组织邀请不算成员。组织检查使用已认证成员 API，包含非公开成员关系。OAuth 请求 `read:org` 权限；组织限制或 SSO 可能需要管理员额外批准。GitHub 请求失败时拒绝登录。

所有获准用户拥有 **WebUI 完整管理员权限**，并非多角色权限系统。会话以不可变的 GitHub 数字 ID 标识为 `github:<id>`；用户白名单按用户名匹配，改名后应复核白名单。显式用户白名单有意允许非组织成员登录。

登录流程包含浏览器绑定的 HttpOnly state cookie、PKCE S256、短期且只能使用一次的状态和会话票据。GitHub access token 只在服务端使用，不持久化；重定向 URL 不包含令牌。AstrBot 会话一小时后过期，下次登录重新检查名单和成员资格；移出组织或名单后，已有会话最多仍可使用一小时。重启会清除进行中的登录。临时状态在内存中，仅支持单进程/单副本部署，与现有 SQLite 部署方式一致。

`password_login_enabled: false` 禁用密码登录、初始账户设置和桌面自动登录，并拒绝已有的本地登录会话。已有的权限受限 API key 仍可使用，例如 Alertmanager；短期插件资源令牌仍受原资源路径限制。设为 `true` 可同时保留上游密码登录。OAuth 使用 GitHub 自身的 MFA，不经过 AstrBot 的密码 TOTP 流程。

名单错误或 GitHub 故障时，由部署管理员修改 YAML 并重启。需要恢复密码登录时设为 `true`，必要时按上游流程重置密码。系统不会自动开放免认证后门。

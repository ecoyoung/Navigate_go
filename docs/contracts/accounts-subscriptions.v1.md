# accounts-subscriptions.v1

该契约定义本地 SQLite 账号、会话和领域订阅的稳定边界。账号系统保持领域无关，美妆只是 `domains` 中的首个配置。

## 数据表

### `users`

- `email`：去空格并小写化后的唯一登录名。
- `display_name`：前台显示名称。
- `password_hash`：scrypt 参数、随机盐和派生值；禁止保存或记录明文密码。
- `role`：`admin` 或 `member`。
- `is_active`：账号是否可登录。
- `must_change_password`：首次管理员临时密码更换标记。

### `auth_sessions`

- 客户端持有随机会话令牌，服务端只保存其 SHA-256 哈希。
- 会话默认有效期 7 天；`revoked_at` 非空或过期后均不可认证。
- Cookie 名为 `navigate_session`，属性为 `HttpOnly`、`SameSite=Lax`。

### `user_subscriptions`

- 唯一键：`user_id + domain_id + delivery_type`。
- `delivery_type` 当前支持 `daily_brief`，可在不改账号模型的前提下扩展。
- `status` 为 `active` 或 `paused`；暂停保留用户选择和审计时间。

## API

- `POST /api/v1/auth/register`：管理员初始化后注册普通账号并登录。
- `POST /api/v1/auth/login`：登录并设置会话 Cookie。
- `POST /api/v1/auth/logout`：撤销当前会话。
- `GET /api/v1/auth/me`：读取当前账号。
- `POST /api/v1/auth/change-password`：改密并撤销该账号全部旧会话。
- `GET /api/v1/subscriptions`：读取当前账号订阅。
- `PUT /api/v1/subscriptions/{domain_key}`：创建、启用或暂停领域订阅。

首个管理员只能通过本机 `scripts.create_admin` 创建，禁止提供匿名 HTTP 首管理员入口。当前版本只记录订阅意愿，不包含邮件投递、支付或外部消息发送。

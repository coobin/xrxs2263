# 薪人薪事到 263 邮箱同步服务

这是一个适合 Docker 部署的同步服务，用于把薪人薪事中的组织部门和员工信息同步到 263 企业邮箱通讯录。

项目默认采用保守同步策略：只处理能通过邮箱账号匹配上的用户；263 中已有但薪人薪事里匹配不到的账号会被跳过，不会禁用或删除。

## 功能

- 从薪人薪事读取部门和员工。
- 从 263 邮箱读取现有部门和用户。
- 自动创建或更新 263 部门。
- 自动创建或更新 263 用户。
- 支持员工离职/停用后禁用对应 263 账号，前提是该员工能匹配到 263 账号。
- 未匹配到薪人薪事员工的 263 账号默认跳过，不禁用、不删除。
- 支持姓名例外名单，适合保留 263 中少数重名用户的备注显示名。
- 支持 263 限流错误 `-1042` 自动退避重试。
- 容器启动时自动同步一次，之后按配置周期定时同步。
- 提供 `/healthz`、`/config`、`/sync`、`/sso-url` 接口。
- 使用 SQLite 保存部门和用户映射状态。
- 输出中文同步摘要日志，方便排查每轮同步结果。

## 对接接口

### 263 邮箱

本项目对接新版 263 JSON API 文档：`263云邮API开放接口文档V20200924.pdf`。

- 默认地址：`https://macom.263.net/api/mail/v2`
- 请求方式：`POST JSON`
- 签名方式：移除 `sign` 和空值字段，按字段名排序，紧凑 JSON 序列化，拼接密钥后取 MD5 小写值
- 部门接口：`/depts/get`、`/depts/create`、`/depts/update`、`/depts/delete`
- 用户接口：`/user/list`、`/user/create`、`/user/update`、`/user/modpwd`、`/user/delete`

注意：

- 这不是旧版 SOAP/WSDL 接口。
- `MAIL263_ACCOUNT` 请填写 263 后台显示的 API 账号，很多租户这里可能是企业域名。
- 263 会校验调用方出口 IP；如果返回 `errcode=-1007`，需要把 Docker 主机的公网出口 IP 加入 263 API 白名单。

### 薪人薪事

薪人薪事侧使用 OAuth token 和签名 JSON 请求模式。

- 默认地址：`https://api.xinrenxinshi.com`
- token 接口：`POST /authorize/oauth/token`
- 部门接口：`POST /v5/department/list`
- 员工接口：`POST /v5/employee/list`
- 签名方式：`HMAC-SHA1(AppSecret, raw_json_body)` 后 `base64`，再 URL encode 放到 `sign` 查询参数

如果你的租户需要自定义 `companyId` 或不同接口路径，可以通过 `.env` 配置调整。

## 快速开始

复制配置模板：

```bash
cp .env.example .env
```

编辑 `.env`，填入薪人薪事和 263 的接口凭据。

首次建议使用干跑模式：

```env
DRY_RUN=true
```

启动服务：

```bash
docker compose -f docker-compose.yml.example up --build
```

手动触发一次同步：

```bash
curl -X POST http://127.0.0.1:8000/sync
```

确认返回结果和后台变化符合预期后，再切换正式同步：

```env
DRY_RUN=false
SYNC_INTERVAL_MINUTES=60
TIMEZONE=Asia/Shanghai
```

然后重启容器。

## 重要配置

- `DRY_RUN`
  是否干跑。`true` 时只计算会发生的变化，不真正写入 263。

- `SYNC_INTERVAL_MINUTES`
  定时同步间隔，生产环境建议先使用 `60`。

- `SYNC_USERID_MODE=email_localpart`
  默认使用邮箱前缀匹配 263 账号，例如 `zhangsan@example.com` 会匹配 `zhangsan`。

- `SYNC_DISABLE_ABSENT_USERS=false`
  未在薪人薪事中匹配到的 263 账号默认不处理。

- `SYNC_DELETE_ABSENT_USERS=false`
  默认不删除 263 账号。除非非常确定，否则不建议打开。

- `SYNC_NAME_PRESERVE_USERIDS=user1,user2`
  姓名保护名单。名单内用户保留 263 当前显示名，不被薪人薪事姓名覆盖。

- `SYNC_DEFAULT_PASSWORD`
  新建 263 邮箱账号时使用的初始明文密码。程序会在请求 263 时自动转成 MD5。

- `SYNC_FORCE_CHANGE_PASSWORD=true`
  新用户首次登录后要求修改密码。

- `MAIL263_GID`
  263 邮箱组 ID，请按你的 263 租户实际配置填写。

- `MAIL263_RETRY_MAX_ATTEMPTS=5`
  263 返回限流错误 `-1042` 时的最大重试次数。

- `MAIL263_REQUEST_INTERVAL_SECONDS=0.3`
  每次调用 263 API 之间的最小间隔，用于降低触发限流的概率。

- `MAIL263_PARTNER_ID` / `MAIL263_AUTH_CORP_ID` / `MAIL263_SSO_KEY`
  仅在使用 `/sso-url` 生成 263 单点登录链接时需要。

## 同步规则

### 部门

- 会按薪人薪事部门树同步到 263。
- 如果 263 中已有相同名称和相同父级的部门，会复用该部门。
- 如果找不到匹配部门，会创建新部门。
- 当前不会删除 263 中多余的旧部门。

### 用户

- 只处理能通过邮箱账号匹配上的用户。
- 现有用户只同步姓名、部门、启用/禁用状态。
- 不同步职位、手机、电话。
- 不匹配薪人薪事的 263 账号会跳过，不禁用、不删除。
- 如果用户在薪人薪事中是离职/停用状态，并且能匹配到 263 账号，则会禁用该 263 账号。
- 新用户创建时使用 `SYNC_DEFAULT_PASSWORD`，请求 263 前自动转 MD5。

## API

- `GET /healthz`
  健康检查。

- `GET /config`
  查看脱敏后的主要配置。

- `POST /sync`
  立即触发一次同步。

- `GET /sso-url?email=user@example.com`
  生成 263 邮箱 SSO 登录链接，需要先配置 SSO 相关参数。

## 日志

每轮同步会输出中文摘要，例如：

```text
开始同步：dry_run=False
同步完成：...
本轮同步摘要：部门新增 0，部门更新 0；用户新增 0，用户更新 0，用户禁用 0；源端部门 33、用户 138；目标端部门 35、用户 149
本轮跳过的未匹配 263 账号：admin@example.com; service@example.com；其余 3 条未展开
```

如果 263 返回限流错误 `-1042`，日志中会输出接口路径、重试次数和等待时间。

## 部署建议

1. 先用 `DRY_RUN=true` 跑一次。
2. 检查 `/sync` 返回结果。
3. 检查 263 后台部门和用户是否符合预期。
4. 确认无误后切换 `DRY_RUN=false`。
5. 生产环境建议先保持 `SYNC_DISABLE_ABSENT_USERS=false` 和 `SYNC_DELETE_ABSENT_USERS=false`。
6. 观察几轮定时同步日志后，再决定是否开启更激进的账号处理策略。

## 安全说明

- 不要提交 `.env`。
- `.env.example` 只放占位配置。
- 263 密钥、薪人薪事 AppKey/AppSecret、默认初始密码都应通过私有 `.env` 管理。
- 本项目的 `.gitignore` 和 `.dockerignore` 已默认排除 `.env`、`.venv`、SQLite 状态库等本地文件。

## 许可证

MIT

# `g2v_dist` — 角色包兑换码分发服务与发码工具

`g2v_dist.py` 是一个仅依赖 Python 标准库（兼容 Python 3.10–3.13）的单文件轻量级角色包分发服务与命令行管理工具（含 `serve`、`add-package`、`remove-package`、`new-code`、`list`、`revoke` 子命令）。服务默认只监听本机回环地址 `127.0.0.1`，通过已有的 Cloudflare Tunnel 与 nginx 反向代理对外提供服务；客户端只需粘贴一串包含服务地址与随机密钥的兑换码，即可查询并下载被授权的角色包。

---

## 1. 部署步骤

### 1.1 创建专用系统用户与目录

在 Linux 服务器上创建无登录权限的非特权用户、程序目录与状态目录：

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin g2v-dist
sudo mkdir -p /opt/g2v-dist /var/lib/g2v-dist
sudo chown -R g2v-dist:g2v-dist /var/lib/g2v-dist
sudo chmod 750 /var/lib/g2v-dist
```

将 `scripts/g2v_dist/g2v_dist.py` 复制到 `/opt/g2v-dist/`：

```bash
sudo cp scripts/g2v_dist/g2v_dist.py /opt/g2v-dist/
```

### 1.2 配置并启动 systemd 服务

复制 `g2v-dist.service` 到 `/etc/systemd/system/g2v-dist.service`（其启动命令为 `python3 /opt/g2v-dist/g2v_dist.py serve --host 127.0.0.1 --port 8765 --prefix /_g2v --state-dir /var/lib/g2v-dist`）并启用：

```bash
sudo cp scripts/g2v_dist/g2v-dist.service /etc/systemd/system/g2v-dist.service
sudo systemctl daemon-reload
sudo systemctl enable --now g2v-dist.service
sudo systemctl status g2v-dist.service
```

### 1.3 配置 nginx 与 Cloudflare Tunnel

在已接入 Cloudflare Tunnel 的站点 `server { ... }` 块中加入 `nginx-location.conf` 的内容（将 `/_g2v/` 转发到 `http://127.0.0.1:8765`），然后重载 nginx：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

此时外部根地址即为 `https://example.com/_g2v`。

---

## 2. 上架与发码流程

服务与管理命令均集成在单文件 `g2v_dist.py` 中，可在服务器上直接运行 `python3 /opt/g2v-dist/g2v_dist.py <子命令> ...`（或切换到 `/opt/g2v-dist` 后运行 `python3 g2v_dist.py <子命令> ...`），也可在仓库根目录运行 `python -m scripts.g2v_dist.g2v_dist <子命令> ...`。默认读取环境变量 `G2V_DIST_STATE_DIR` 或 `/var/lib/g2v-dist`，也可通过 `--state-dir` 指定。

### 2.1 上架角色包（`add-package`）

角色包 `.zip` 内任意层级必须包含 `manifest.json`：

```bash
sudo -u g2v-dist python3 /opt/g2v-dist/g2v_dist.py \
  --state-dir /var/lib/g2v-dist \
  add-package /path/to/heroine_pack.zip --id heroine --name "示例角色"
```

- `--id` 可选，省略时自动从文件名清理生成（仅保留 `[a-z0-9_-]{1,64}`）。
- `--name` 可选，省略时读取压缩包内 `manifest.json` 的 `name` 字段，若无则回退为包 ID。
- 上架后会在状态目录生成 `packages/<id>.zip` 与 `packages/<id>.json`（记录显示名、字节数、`sha256`、上架时间）。

### 2.2 下架角色包（`remove-package`）

```bash
sudo -u g2v-dist python3 /opt/g2v-dist/g2v_dist.py \
  --state-dir /var/lib/g2v-dist \
  remove-package heroine
```

### 2.3 生成兑换码（`new-code`）

```bash
sudo -u g2v-dist python3 /opt/g2v-dist/g2v_dist.py \
  --state-dir /var/lib/g2v-dist \
  new-code \
  --server-url https://example.com/_g2v \
  --packages heroine \
  --max-downloads 3 \
  --expires-days 30 \
  --note "给朋友 A"
```

- 若允许下载所有已上架角色包，传 `--all`（或省略 `--packages`）。
- 命令只在标准输出打印一次完整兑换码（如 `G2V1.<...>.<...>`）；数据库中仅保存密钥的 SHA-256 哈希，无法再次导出明文兑换码。

### 2.4 查看角色包与兑换码列表（`list`）

```bash
sudo -u g2v-dist python3 /opt/g2v-dist/g2v_dist.py --state-dir /var/lib/g2v-dist list
```

输出已上架的角色包信息以及各兑换码的 8 位公开编号、状态（有效/已过期/已作废）、允许的包、每个包的剩余下载次数、过期时间与备注，不显示任何密钥或完整哈希。

### 2.5 作废兑换码（`revoke`）

```bash
sudo -u g2v-dist python3 /opt/g2v-dist/g2v_dist.py \
  --state-dir /var/lib/g2v-dist \
  revoke a1b2c3d4
```

---

## 3. 兑换码格式规范

完整兑换码由三段组成，以英文句点 `.` 分隔：

```text
G2V1.<base64url(服务器根地址)>.<base64url(32 字节随机密钥)>
```

- **第一段**：固定版本标识 `G2V1`。
- **第二段**：服务器根地址（UTF-8 编码，去除末尾 `/`）的 URL-safe Base64 编码，**不带 `=` 补齐**。例如 `https://example.com/_g2v`。
- **第三段**：32 字节（256 位）密码学安全随机数的 URL-safe Base64 编码（固定 43 个字符），**不带 `=` 补齐**。该段字符串即为 HTTP 接口使用的 `<base64url 密钥>`（`code_secret`）。

`g2v_dist.py` 提供无副作用的纯函数供服务端与客户端复用：

- `encode_code(url, secret) -> str`：校验并规范化 `http://` 或 `https://` 服务器根地址与 32 字节密钥（支持传入 32 字节 `bytes` 或对应的 base64url 字符串），返回完整兑换码。
- `parse_code(text) -> ParsedCode`：自动忽略首尾空白与换行，校验三段格式、版本前缀、无补齐 canonical base64url 编码、URL 合法性及 32 字节密钥长度；返回 `ParsedCode(server_url, code_secret)`（既支持 `url, secret = parse_code(text)` 解包，也支持 `.server_url` / `.code_secret` 属性访问）。格式不合法时抛出带中文说明的 `CodeFormatError`（`ValueError` 子类）。

---

## 4. HTTP 接口规范

所有错误响应均为 UTF-8 JSON `{"error": "<中文说明>"}`。假设路径前缀为 `/_g2v`：

1. **`POST /_g2v/v1/redeem`**
   - 请求体（上限 4 KiB）：`{"code_secret": "<base64url 密钥>"}`
   - 成功 `200`：`{"packages": [{"id": "...", "name": "...", "size": 1234, "sha256": "...", "remaining": 3}]}`，仅返回该兑换码允许下载且剩余次数 `remaining > 0` 的角色包。
   - 兑换码不存在、已过期或已作废：统一返回 `401` `{"error": "兑换码无效或已过期"}`。
   - 触发 IP 限流：返回 `429` `{"error": "请求过于频繁，请稍后再试"}`，响应头包含 `Retry-After`。

2. **`GET /_g2v/v1/packages/<id>`**
   - 请求头：`Authorization: Bearer <base64url 密钥>`，可选单段 `Range: bytes=start-` 或 `Range: bytes=start-end`。
   - 从第 0 字节开始的请求（无 `Range` 或 `Range: bytes=0-...`）计一次下载，在发送文件流之前于 SQLite 中原子加一；若次数已用尽，返回 `403` `{"error": "这个角色包的下载次数已用完"}`。
   - 从中间字节开始的请求（`start > 0`，断点续传）不扣减下载次数，但要求该兑换码对该角色包最近一次计次下载发生在 24 小时之内，否则返回 `403`。
   - 成功返回 `200` 或 `206`，包含 `Content-Length`、`Accept-Ranges: bytes`、`ETag`（包 `sha256`）以及 `Cache-Control: private, no-store`，按 64 KiB 分块流式发送，不将整份压缩包读入内存。

---

## 5. 安全说明

- **默认仅监听本机回环**：`g2v_dist.py serve` 默认绑定 `127.0.0.1`，除非显式传入 `--allow-public`，否则拒绝绑定任何非回环地址。对外访问由 Cloudflare Tunnel 与本机 nginx 转发。
- **禁止边缘缓存（`Cache-Control: private, no-store`）**：所有接口与下载响应（含 `200` 和 `206`）均携带 `Cache-Control: private, no-store`，防止 Cloudflare 等反向代理或 CDN 在开启全站缓存规则时将需要鉴权的角色包响应缓存到边缘节点。
- **密钥只存单向哈希**：密钥本身为 256 位随机数，数据库 `dist.db` 只存储其 SHA-256 摘要并按 `secret_hash` 精确查询；访问日志与管理命令仅使用哈希前 8 位作为公开编号，绝不记录密钥。
- **防路径穿越与包体限制**：角色包 ID 严格匹配 `^[a-z0-9_-]{1,64}$`，包含 `..`、`/`、`\` 的路径一律返回 `404`，绝不读取状态目录之外的文件；POST 请求体上限为 4 KiB。
- **可信代理 IP 提取与失败限流**：仅当 TCP 对端为回环地址（如本机 nginx）时才按优先级读取 `CF-Connecting-IP` 与 `X-Forwarded-For` 首项；若对端为非回环地址则直接取连接对端 IP，防止伪造请求头绕过限流。同一来源 IP 在 1 小时窗口内累计 `401` 失败超过 10 次（即第 11 次失败后），自动封禁该 IP 1 小时并返回 `429` 与 `Retry-After`。

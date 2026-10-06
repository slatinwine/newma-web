# Newma Chat — 清水混凝土风格的本地聊天页

对接本地 [newma CLI](https://www.npmjs.com/package/newma-cli)（牛码）自带的
`--web` API 服务器模式，清水混凝土（素水泥灰 + 炭灰 + 细颗粒质感）风格聊天界面。

## 启动

```bash
cd ~/newma-web
./start.sh          # 默认端口 3010，或 ./start.sh 8080
```

浏览器打开 **http://127.0.0.1:3010/**

> 首次对话报 404 时，是因为 newma 会把 baseUrl 误拼成 `/v4/v1/chat/completions`，
> `start.sh` 已通过 `OPENAI_ENDPOINT` 环境变量修复。如换其他模型服务商，
> 改 `~/.kode/settings.json` 后同步改 `start.sh` 里的端点。

## 结构

- `public/index.html` — 单文件聊天前端（无构建、无依赖）。
- `server.py` — Python 桥接服务器（仅标准库）：伺服页面、提供
  `/api/local/skills` 与 `/api/local/plugins` 两个本地目录 API、
  把 `/health` `/api/status` `/api/execute` `/api/clear` 反代到内部 newma。
- `start.sh` — 一键启动：内部 `newma --web`（3011，仅本机）+ 对外桥（3010）。

## 侧栏入口

- **🧩 技能库**：读取 `~/.kode/skills/` 与项目 `.kode/skills/` 下各技能的
  `SKILL.md` front-matter（name / description）。点击技能卡片会把
  `请使用 xx 技能处理以下需求：` 填入输入框。
- **🛍 插件市场**：列出项目 `.kode/plugins/` 已装插件与 newma 内置插件；
  空态时提示 `newma-create-plugin` 创建方法。

## 工作原理

newma `--web` 模式的协议（`src/loop/frontends/web-frontend.ts`）：

| 端点 | 用途 |
|---|---|
| `GET /` | 伺服 `public/index.html`（本页面前端） |
| `POST /api/execute` | 提交消息，body: `{"requirement":"...","mode":"chat"}` |
| `GET /api/status` | 轮询 `outputBuffer`（每行一个 JSON 事件） |
| `GET /health` | 健康检查（侧栏底部状态点） |
| `POST /api/clear` | 清空输出缓冲（前端自动管理，一般无需手动调） |

前端轮询 `/api/status` 增量消费 `outputBuffer`：`type:"output"` 且内容
**不以换行开头** 的是 AI 正文；`\n` 开头的条目是服务器 UI 噪音（banner、
💬 Chat 标题等），前端过滤；`✅ Done` 表示本轮结束。

## 多轮上下文

newma 的 chat 模式对每条输入独立处理（不带历史）。前端做了两件事：

1. 把最近 10 条会话历史拼进请求（`【此前对话记录】…【新消息】…`）；
2. 消息以 `/chat ` 前缀发送 —— 走 newma 的命令路由强制聊天模式，
   避免带历史的消息被意图识别当成编码任务而进入规划循环。

## 注意

- **单服务器会话**：多轮上下文由前端拼接实现，左侧"会话列表"只是本地
  聊天记录归档；切换/新建会话不影响服务器端。
- **`/api/stop` 勿从页面调用**：它会停掉整个 newma 服务。前端的"停止"
  按钮只停止接收显示，服务端会继续跑完。
- newma `--web` 默认权限级别为 dangerous（自动执行命令）。纯聊天（`/chat`
  前缀）不触发工具执行，但请只在可信的本机环境暴露该端口。
- `index.html` 也可单独拷走用任意静态服务器托管，或直接双击打开
  （内置 CORS 支持；打开后在 ⚙ 设置里填 `http://127.0.0.1:3010`）。


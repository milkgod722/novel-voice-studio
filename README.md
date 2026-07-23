# 小说声工坊

用一段**已获本人授权**的日常语音克隆音色，把 TXT / Markdown / EPUB 小说生成富有感情的 WAV 有声书。数据默认只保存在本机。

## 已实现

- 浏览器上传微信导出的 WAV / MP3 / M4A / OGG / WebM；自动转为 24 kHz 单声道并做响度、带宽标准化。
- 中文编码识别、EPUB 正文抽取、章节识别、长文本按语义断句。
- 每个片段从文本规划 8 维情感（快乐、愤怒、悲伤、恐惧、厌恶、忧郁、惊讶、平静）。
- IndexTTS2 音色/情感解耦推理适配器；单 GPU 串行队列，片段缓存可用于失败续跑。
- WAV 无损拼接、进度轮询、网页试听/下载、合成来源 `provenance.json`。
- 强制授权确认；不把声音或小说上传到第三方。

> 演示引擎会生成可听见的测试音，不会克隆声音。它用来快速验证完整产品流程。真实克隆需要按下文安装 IndexTTS2。

## 1. 运行应用（演示模式）

Windows PowerShell：

```powershell
uv sync --extra test --python 3.11
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。不要使用系统现有的 Python 3.14；PyTorch/IndexTTS2 使用独立的 Python 3.11 环境。

## 2. 启用真实音色克隆

先安装模型（权重较大，需要稳定网络）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_indextts.ps1
```

IndexTTS2 维护独立环境。最稳妥的部署方式是在它的环境中补装本项目 Web 依赖，再从项目根目录启动：

```powershell
cd IndexTTS2
uv pip install -e ..
$env:NVS_ENGINE="indextts2"
$env:INDEXTTS_PATH=(Get-Location).Path
$env:INDEXTTS_MODEL_DIR=(Join-Path (Get-Location).Path "checkpoints")
$env:PYTHONPATH=(Split-Path (Get-Location).Path -Parent)
uv run --no-sync uvicorn app.main:app --app-dir .. --host 127.0.0.1 --port 8000
```

本机检测到 RTX 4090 Laptop 16 GB；代码默认 FP16、关闭 DeepSpeed 和自定义 CUDA kernel，优先保证 Windows 可用性。首次加载模型会较慢。

## 语音素材建议

- 10–30 秒，单人、无音乐、无明显回声；自然聊天比刻意压嗓更好。
- 微信原生 `.silk` 不是通用音频容器，先用可信工具在本机转成 WAV；微信导出的 M4A 可直接上传。
- 参考音频决定音色，小说文字逐句决定情绪。情感强度建议从 0.65 开始；过高容易损失音色相似度和清晰度。
- 只克隆自己的声音，或取得声音本人对具体用途的明确授权。公开发布时应说明音频由 AI 合成。

## 测试

```powershell
uv run pytest
```

测试覆盖文本/章节切分、情感向量、授权门禁、音频标准化、完整业务服务与 HTTP 上传→任务→WAV 下载链路。真实模型的声学验收需要一段真人参考音，执行：

1. 以真实引擎启动应用并上传 10–30 秒参考音；
2. 用固定的中性、开心、悲伤、紧张各 3 句生成测试集；
3. 人工盲听 MOS（自然度、音色相似度、情感匹配度，各 1–5 分），建议上线门槛均值 ≥ 3.8；
4. 用 ASR 回转录计算 CER，建议普通叙述 ≤ 8%，强情感对白 ≤ 12%；
5. 未达到门槛时优先清理参考音噪声，其次降低情感强度，最后调整断句长度。

## API

- `GET /api/health`：引擎状态
- `POST /api/voices`：上传参考语音（multipart: `file`, `name`, `consent`）
- `POST /api/books`：上传小说（multipart: `file`, `title`）
- `POST /api/jobs`：创建生成任务
- `GET /api/jobs/{id}`：查询进度
- `GET /api/jobs/{id}/audio`：试听/下载

## 生产化建议

当前版本适合单机个人使用。若要开放给多人，必须增加登录、配额、加密存储、删除权、审计、显式合成标识与滥用检测；不要直接把本地服务暴露到公网。IndexTTS2 的模型许可包含使用限制，商业发布前应重新审阅其当前许可证并咨询法律意见。

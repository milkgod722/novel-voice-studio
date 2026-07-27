# 小说声工坊

用一段**已获本人明确授权**的日常语音和 TXT / Markdown / EPUB 小说，通过远程 TTS / 声音复刻 API 生成有声书。生成结果可以直接在网页播放；默认下载体积更小的 64 kbps MP3，也可选择无损 WAV。

项目不再加载本地语音模型。API Key、SecretKey、Access Token 和 Voice Key 只保存在当前服务进程内，不写入磁盘；服务重启后需要重新填写。

## 已支持的远程服务

| 服务 | 参考声音使用方式 | 情感控制 | 页面需要填写 |
|---|---|---|---|
| 小米 MiMo（默认） | 每次请求直接发送上传的参考 WAV | 中文演播指令 | URL、模型、Key、鉴权方式 |
| 阿里云 CosyVoice | 先在阿里创建克隆音色 ID | v3.5 克隆音色支持 instruction | URL、模型、DashScope Key、音色 ID |
| 腾讯云语音合成 | 先完成声音复刻，取得 FastVoiceType | `EmotionCategory` + `EmotionIntensity` | URL、SecretId、SecretKey、FastVoiceType |
| 百度智能云声音复刻 | 先创建 `voice_id` | happy/down/surprise/angry/fear/disgust | URL、Authorization Key、voice_id |
| Google Cloud TTS | 先生成 Instant Custom Voice cloning key | 文本韵律、停顿和语速能力 | URL、OAuth Token、Project ID、cloning key |
| OpenAI TTS | 使用已有 `voice_*` 自定义音色或预置 voice | `instructions` 演播指令 | URL、模型、API Key、Voice ID |
| IndexTTS URL | 每次 multipart 请求直接发送参考 WAV | 原生 8 维 `emo_vector` | URL、模型、可选 Key |

“支持”不等于可以绕过厂商的产品开通、音色注册或授权流程。Google Instant Custom Voice 目前需要白名单，OpenAI Custom Voice 仅向符合条件的客户开放；两者都要求规定的同意录音。阿里、腾讯和百度需要先创建音色 ID，再由本项目执行长文本分片合成。

任何模型都不能技术上保证“完美复刻”。实际音色相似度和感情表现取决于参考音质量、厂商模型、音色权限、语言、文本和情感参数。本项目会按厂商支持能力传递情感，而不会把预置音色伪装成声音克隆。

## 运行

Windows PowerShell：

```powershell
uv sync --extra test --python 3.11
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>：

1. 页面顶部选择远程语音服务并填写相应凭据。
2. 上传 10–30 秒、单人、无背景音乐的参考语音，并确认授权。
3. 导入小说，先生成前 300 字试听。
4. 选择 MP3（默认）或 WAV，在作品台查看进度，完成后直接播放或下载。

不要把本地服务直接暴露到公网。真实凭据不要写进代码、README、`.env` 或 Git 提交。

## 各厂商配置说明

### 小米 MiMo

默认使用：

```text
协议：mimo-chat
URL：https://api.xiaomimimo.com/v1/chat/completions
模型：mimo-v2.5-tts-voiceclone
鉴权：api-key
```

兼容网关也可填写自己的 URL、模型和 Bearer / `api-key`。官方小米 URL 要求开放平台按量调用的 `sk-` Key，Token Plan 的 `tp-` Key 不适用于该小说应用。

### 阿里云 CosyVoice

项目调用非流式 `SpeechSynthesizer` HTTP API并请求 WAV。页面中的音色 ID 必须提前通过阿里 [Voice Cloning API](https://help.aliyun.com/en/model-studio/cosyvoice-clone-api-reference) 创建。推荐使用支持克隆音色 instruction 的 `cosyvoice-v3.5-flash`。

### 腾讯云

项目直接实现 TC3-HMAC-SHA256 签名并调用 `TextToVoice`。一句话复刻音色使用 `VoiceType=200000000`，页面填写 `FastVoiceType`。SecretId 与 SecretKey 分开填写，均不落盘。

### 百度智能云

页面填写已经创建的数字 `voice_id`。项目调用非流式声音复刻 TTS，强制请求 24 kHz WAV，并把文本情感映射为百度支持的 emotion 值。

### Google Cloud Text-to-Speech

页面 Key 字段填写短期 OAuth Access Token，不是服务账号 JSON；Project ID 单独填写。中文 Instant Custom Voice 的官方语言代码是 `cmn-CN`。Voice Key 是通过 [Chirp 3 Instant Custom Voice](https://docs.cloud.google.com/text-to-speech/docs/chirp3-instant-custom-voice) 授权流程生成的 cloning key。

### OpenAI TTS

默认调用 `POST /v1/audio/speech`，模型为 `gpt-4o-mini-tts`。自定义音色填写 `voice_*` ID；如果账号没有 Custom Voice 权限，也可填写预置 voice，但那不是对上传声音的克隆。自定义音色的创建需要 consent recording，参见 [OpenAI Audio API](https://developers.openai.com/api/reference/resources/audio/subresources/voices/methods/create)。

### IndexTTS URL 合约

IndexTTS 官方仓库没有规定统一 HTTP 服务，因此本项目定义以下 multipart 请求：

| 字段 | 内容 |
|---|---|
| `spk_audio_prompt` | 24 kHz 单声道参考 WAV |
| `text` | 当前小说分片 |
| `emo_vector` | JSON 数组 `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]` |
| `emo_alpha` | `0.65` |
| `use_random` | `false` |
| `model` | 页面填写的模型名 |

服务可以直接返回 `audio/wav`，也可以返回 JSON 中的 Base64 `audio` / `audio_data` / `data`，或音频 `url`。

## 稳定性与隐私

- 前端明确分为创作设置、生成参数、作品与进度三大区域；创作设置按“模型 → 声音 → 小说”逐项显示状态，并自动引导到下一项必要操作。
- 同名声音或小说会在下拉框追加来源文件与短 ID，避免误选旧素材；静态资源带版本号，升级后不会混用旧缓存。
- 上传音频统一转为 24 kHz、单声道、16-bit PCM WAV。
- 长文本按厂商单次上限切分；腾讯 140 字、百度/阿里 450 字，其他服务使用更长分片。
- 相邻分片的 8 维情感向量采用 15% / 70% / 15% 邻接平滑；MiMo 使用较低随机性和统一叙述状态指令。
- 合并时使用 40ms 短交叉淡化，不再在每个片段间硬插入 180ms 静音。
- 长篇任务按章节优先渐进发布；每个任务可选择约 1000 / 2000 / 5000 字一段，提交前显示所选章节字数与预计分段数。
- 作品台每个任务只创建一个分段播放器，通过下拉框切换已完成段，避免数百段长篇生成数百个 `<audio>` 控件拖慢页面。
- 播放期间暂停自动轮询刷新，避免正在收听的音频被 DOM 重建打断；长任务运行时仍可继续提交，后续任务自动排队。
- 全部分段完成后直接对同规格 MP3 做快速无损拼接，不再为了完整版本重新生成巨大的临时 PCM WAV。
- HTTP 429、5xx、响应截断、连接重置和超时会指数退避重试。
- 未发布分段的 WAV 分片落盘缓存以支持断点恢复；一个渐进分段发布后立即清理其临时 WAV，重试时直接跳过已发布分段，避免长篇持续膨胀。
- 默认成品为 24 kHz 单声道 64 kbps MP3，通常约为原始 16-bit PCM WAV 的六分之一；需要无损归档时可选 WAV。
- 任务可取消、重试和删除；删除会移除作品及其全部音频文件。
- 完成的作品通过 `/audio` 路由在网页内播放，通过 `/download` 下载。
- `data/`、`.env`、用户音频、小说和 API 凭据不会进入 Git。

## 测试

```powershell
uv run pytest
```

自动化测试覆盖：

- 文本、章节、50 万字分段压力、情感向量和音频管线；
- HTTP 上传、生成、播放、下载、取消、恢复和删除，以及已发布分段的缓存清理与断点跳过；
- 小米响应截断重试；
- 阿里请求与音频 URL 下载；
- 腾讯 TC3 签名、复刻音色和情感参数；
- 百度二进制 WAV 与 emotion；
- Google cloning key；
- OpenAI Custom Voice 和 instructions；
- IndexTTS multipart 参考音与 8 维情感合约。

真实声学验收仍需各厂商的有效账户、已授权音色和计费权限。建议固定一组中性、开心、悲伤、紧张文本，分别评估自然度、音色相似度、情感匹配度和 ASR 回转录 CER。

## API

- `GET /api/health`：当前引擎状态
- `POST /api/config/voice-clone`：配置远程服务
- `POST /api/config/mimo`：向后兼容别名
- `POST /api/voices`：上传参考语音（`file`, `name`, `consent`, 可选 `transcript`）
- `POST /api/books`：上传小说
- `POST /api/jobs`：创建试听或完整任务
- `GET /api/jobs/{id}`：查询进度
- `POST /api/jobs/{id}/retry`：从已有分片继续
- `POST /api/jobs/{id}/cancel`：取消任务
- `DELETE /api/jobs/{id}`：删除作品和音频
- `GET /api/jobs/{id}/audio`：网页播放
- `GET /api/jobs/{id}/download`：下载所选格式（默认 MP3）
- `GET /api/jobs/{id}/segments/{index}/audio`：播放已经完成的渐进分段
- `GET /api/jobs/{id}/segments/{index}/download`：下载单个分段
